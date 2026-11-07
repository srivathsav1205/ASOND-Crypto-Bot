#!/usr/bin/env python3
"""
Binance Spot pump scalper
-------------------------
* Scans 1m and 1s klines of every USDT spot pair (bStocks *BUSDT pairs excluded).
* BUY  : 1m candle up >= PUMP_1M_PCT  OR  1s candle up >= PUMP_1S_PCT  (vs candle open)
* SELL (EXIT_MODE=ladder, default) : ladder stop-loss from the VAMSI bot. Targets +3/5/10/15/25/50%
         above the actual buy price (LADDER_TARGETS_PCT). Reaching a target does NOT sell: the stop
         moves up one target behind (LADDER_STOP_MODE=previous): T1 -> +0.2% (LADDER_FIRST_STOP_PCT),
         T2 -> +2% (T1), T3 -> +5% (T2) ... only ever up. Falling back to the stop sells at market.
         The last target sells 100%. A real STOP_LOSS order on Binance follows the stop
         (LADDER_EXCHANGE_STOP) so it still protects the trade if the PC / bot is off.
         Before Target 1: stop = -STOP_LOSS_PCT if STOP_LOSS_ENABLED, otherwise none (20% hold rule).
* SELL (EXIT_MODE=tp) : limit sell placed immediately at +TAKE_PROFIT_PCT over the average fill price.
* COOLDOWN : a coin is not bought again for SYMBOL_COOLDOWN_SEC (default 48h) after a buy.
             Survives restarts (saved in positions.json).
* HOLD : if price falls HOLD_DRAWDOWN_PCT (default 20%) below the buy price, the TP order
         is cancelled and the coin is kept as a long-term holding (never sold by the bot).
* STOP-LOSS (optional, STOP_LOSS_ENABLED) : market-sell at -STOP_LOSS_PCT (default 3%).
* Telegram notifications + commands: /status /open /holdings /pause /resume /help
"""
import asyncio
import collections
import hmac as _hmac
import hashlib
import hmac
import html
import io
import json
import logging
import os
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from urllib.parse import urlencode

import aiohttp
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────── config ───────────────────────────
def _env(name, default=None):
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else v.strip()

def _bool(name, default):
    return str(_env(name, default)).lower() in ("1", "true", "yes", "on")

API_KEY = _env("BINANCE_API_KEY", "")
API_SECRET = _env("BINANCE_API_SECRET", "")
TG_TOKEN = _env("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = _env("TELEGRAM_CHAT_ID", "")
# More bots / chats that get EVERY message too:  token|chat_id|control  (control or view), comma separated
TG_EXTRA_BOTS = []
for _e in _env("TELEGRAM_EXTRA_BOTS", "").split(","):
    _f = [x.strip() for x in _e.split("|")]
    if len(_f) >= 2 and _f[0] and _f[1]:
        TG_EXTRA_BOTS.append((_f[0], _f[1], (_f[2].lower() if len(_f) > 2 else "control") != "view"))
DASHBOARD_ENABLED = _bool("DASHBOARD_ENABLED", "true")
DASHBOARD_HOST = _env("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(_env("DASHBOARD_PORT", "8091"))       # VAMSI bot uses 8090
DASHBOARD_TOKEN = _env("DASHBOARD_TOKEN", "")              # optional: page asks for it
if DASHBOARD_HOST not in ("127.0.0.1", "localhost") and not DASHBOARD_TOKEN:
    raise SystemExit("DASHBOARD_HOST other than 127.0.0.1 needs a DASHBOARD_TOKEN")
TG_SNAPSHOTS = _bool("TELEGRAM_SNAPSHOTS", "true")          # chart picture on every buy and sell
TG_SUMMARY_MIN = int(_env("TELEGRAM_SUMMARY_MINUTES", "60"))  # activity summary; 0 = off

DRY_RUN = _bool("DRY_RUN", "true")
TRADE_USDT = Decimal(_env("TRADE_USDT", "5"))
PUMP_1M_PCT = float(_env("PUMP_1M_PCT", "5"))
PUMP_1S_PCT = float(_env("PUMP_1S_PCT", "1"))
TAKE_PROFIT_PCT = Decimal(_env("TAKE_PROFIT_PCT", "2"))
MAX_OPEN_TRADES = int(_env("MAX_OPEN_TRADES", "20"))
SYMBOL_COOLDOWN_SEC = int(_env("SYMBOL_COOLDOWN_SEC", str(48 * 3600)))   # 48 hours
# Price drop (from buy price) that turns a trade into a long-term holding (TP cancelled). 0 = off
HOLD_DRAWDOWN_PCT = Decimal(_env("HOLD_DRAWDOWN_PCT", "20"))
# Don't buy a coin again while it sits in the long-term holdings
SKIP_HELD_COINS = _bool("SKIP_HELD_COINS", "true")
# Optional stop-loss: cancel TP and market-sell when price is STOP_LOSS_PCT below buy price
STOP_LOSS_ENABLED = _bool("STOP_LOSS_ENABLED", "false")
STOP_LOSS_PCT = Decimal(_env("STOP_LOSS_PCT", "3"))
# Don't buy coins on Binance's 24h "Losers" list (24h change below MIN_24H_CHANGE_PCT)
SKIP_24H_LOSERS = _bool("SKIP_24H_LOSERS", "true")
MIN_24H_CHANGE_PCT = float(_env("MIN_24H_CHANGE_PCT", "0"))    # 0 = any coin that is down over 24h is skipped
# Exit style:  ladder = ladder stop-loss (VAMSI bot)  |  tp = one limit sell at +TAKE_PROFIT_PCT
EXIT_MODE = _env("EXIT_MODE", "ladder").lower()
if EXIT_MODE not in ("ladder", "tp"):
    raise SystemExit(f"EXIT_MODE must be 'ladder' or 'tp', not '{EXIT_MODE}'")
# Ladder targets in % above the buy price. Reaching one does NOT sell: the stop moves up to it.
LADDER_TARGETS = [Decimal(x.strip()) for x in _env("LADDER_TARGETS_PCT", "3,5,10,15,25,50").split(",")
                  if x.strip()]
if not LADDER_TARGETS or LADDER_TARGETS != sorted(LADDER_TARGETS) or LADDER_TARGETS[0] <= 0:
    raise SystemExit("LADDER_TARGETS_PCT must be increasing positive numbers, e.g. 3,5,10,15,25,50")
# Where the stop goes when a target is hit:
#   previous = one target behind (T1 -> +LADDER_FIRST_STOP_PCT, T2 -> T1 value, T3 -> T2 value, ...)
#   same     = at the target that was just hit (T1 -> +2%, T2 -> +5%, ...)
LADDER_STOP_MODE = _env("LADDER_STOP_MODE", "previous").lower()
if LADDER_STOP_MODE not in ("previous", "same"):
    raise SystemExit(f"LADDER_STOP_MODE must be 'previous' or 'same', not '{LADDER_STOP_MODE}'")
LADDER_FIRST_STOP_PCT = Decimal(_env("LADDER_FIRST_STOP_PCT", "0.2"))   # stop after Target 1 (previous mode)
LADDER_SELL_AT_LAST = _bool("LADDER_SELL_AT_LAST", "true")      # sell 100% at the last target
LADDER_EXCHANGE_STOP = _bool("LADDER_EXCHANGE_STOP", "true")    # real STOP_LOSS order on Binance
MIN_QUOTE_VOL_1S = float(_env("MIN_QUOTE_VOL_1S", "0"))   # USDT traded inside the 1s candle
MIN_QUOTE_VOL_1M = float(_env("MIN_QUOTE_VOL_1M", "0"))   # USDT traded inside the 1m candle

QUOTE = "USDT"
EXCLUDE_SUFFIX = _env("EXCLUDE_SUFFIX", "BUSDT")                # bStocks pattern
KEEP_SYMBOLS = {s.strip().upper() for s in _env("KEEP_SYMBOLS", "BNBUSDT").split(",") if s.strip()}
EXTRA_EXCLUDE = {s.strip().upper() for s in _env("EXTRA_EXCLUDE", "").split(",") if s.strip()}
EXCLUDE_STABLES = _bool("EXCLUDE_STABLES", "true")
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "AEUR", "EURI", "USD1",
           "XUSD", "BFUSD", "RLUSD", "USDE", "PYUSD", "BUSD", "UST", "USTC"}

SYMBOLS_PER_SOCKET = int(_env("SYMBOLS_PER_SOCKET", "100"))  # 2 streams per symbol
POLL_SEC = int(_env("POLL_SEC", "20"))
EXINFO_REFRESH_SEC = 6 * 3600
# live and paper (DRY_RUN) trades are kept in separate files so they can never mix
STATE_FILE = _env("STATE_FILE", "positions_dry.json" if DRY_RUN else "positions.json")

REST = "https://api.binance.com"
WS = "wss://stream.binance.com:9443/stream?streams="

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")],
)
log = logging.getLogger("pumpbot")


# ─────────────────────────── helpers ───────────────────────────
def fmt(d: Decimal) -> str:
    s = format(d.normalize(), "f")
    return s if "." not in s else s.rstrip("0").rstrip(".") or "0"

def floor_step(v: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return v
    return (v / step).to_integral_value(ROUND_DOWN) * step

def ceil_step(v: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return v
    return (v / step).to_integral_value(ROUND_UP) * step

def is_excluded(symbol: str, base: str) -> bool:
    if symbol in KEEP_SYMBOLS:
        return False
    if symbol in EXTRA_EXCLUDE:
        return True
    if EXCLUDE_SUFFIX and symbol.endswith(EXCLUDE_SUFFIX):
        return True
    if EXCLUDE_STABLES and base in STABLES:
        return True
    return False


def render_chart(symbol, timeframe, rows, lines, title_extra="", markers=None):
    """Candles + entry / stop / targets / exit lines as a PNG (same look as the VAMSI bot)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    BG, FG, GRID, UP, DOWN = "#0f1419", "#d6dde6", "#232b36", "#26a69a", "#ef5350"
    cs = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows][-60:]
    fig, ax = plt.subplots(figsize=(9, 5), dpi=110)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    if cs:
        w = (cs[1][0] - cs[0][0]) * 0.7 if len(cs) > 1 else 60_000
        for t, o, h, l, c in cs:
            col = UP if c >= o else DOWN
            ax.vlines(t, l, h, color=col, linewidth=1)
            ax.bar(t, abs(c - o) or (h - l) * 0.02 or o * 1e-4, bottom=min(o, c), width=w, color=col, align="center")
    palette = {"Stop": DOWN, "Entry": "#4da3ff", "Exit": "#ffffff", "TP": "#66bb6a"}
    valid = sorted(((v, k) for k, v in lines.items() if v and v > 0))
    lo = min([x[3] for x in cs] + [v for v, _ in valid]) if (cs or valid) else 0
    hi = max([x[2] for x in cs] + [v for v, _ in valid]) if (cs or valid) else 1
    if hi <= lo:
        hi = lo * 1.01 + 1e-12
    gap, last_y = (hi - lo) * 0.045, None
    for price, label in valid:
        col = palette.get(label, "#9ccc65" if label.startswith("T") else "#b0bec5")
        ax.axhline(price, color=col, linestyle="-" if label == "Entry" else "--", linewidth=1, alpha=0.9)
        y = price if last_y is None or price - last_y >= gap else last_y + gap
        last_y = y
        ax.text(1.002, y, f" {label} {price:.6g}", color=col, fontsize=8, va="center",
                transform=ax.get_yaxis_transform())
    ax.set_ylim(lo - (hi - lo) * 0.03, max(hi, last_y or hi) + (hi - lo) * 0.03)
    for t, pr, label, col in markers or []:
        ax.scatter([t], [pr], color=col, s=70, zorder=5, marker="^" if "BUY" in label else "v")
    ax.set_title(f"{symbol}  {timeframe}   {title_extra}", color=FG, fontsize=11, loc="left")
    ax.tick_params(colors=FG, labelsize=8)
    ax.grid(color=GRID, linewidth=0.5)
    for sp in ax.spines.values():
        sp.set_color(GRID)
    if cs:
        ticks = cs[:: max(1, len(cs) // 6)]
        ax.set_xticks([x[0] for x in ticks])
        ax.set_xticklabels([time.strftime("%H:%M", time.localtime(x[0] / 1000)) for x in ticks])
    fig.text(0.01, 0.01, "Pump bot", color="#5c6773", fontsize=7)
    fig.tight_layout(rect=(0, 0.02, 0.88, 1))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


class BinanceError(Exception):
    def __init__(self, status, payload):
        self.status = status
        self.code = payload.get("code") if isinstance(payload, dict) else None
        self.msg = payload.get("msg") if isinstance(payload, dict) else str(payload)
        super().__init__(f"HTTP {status} code={self.code} {self.msg}")


# ─────────────────────────── Binance REST ───────────────────────────
class Binance:
    def __init__(self, session: aiohttp.ClientSession):
        self.s = session
        self.offset = 0

    async def _req(self, method, path, params=None, signed=False):
        params = dict(params or {})
        headers = {}
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self.offset
            params["recvWindow"] = 5000
            q = urlencode(params)
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            url = f"{REST}{path}?{q}&signature={sig}"
            headers["X-MBX-APIKEY"] = API_KEY
        else:
            url = f"{REST}{path}" + (f"?{urlencode(params)}" if params else "")
        async with self.s.request(method, url, headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=15)) as r:
            text = await r.text()
            try:
                data = json.loads(text)
            except ValueError:
                data = {"msg": text[:200]}
            if r.status != 200:
                raise BinanceError(r.status, data)
            return data

    async def sync_time(self):
        t = await self._req("GET", "/api/v3/time")
        self.offset = t["serverTime"] - int(time.time() * 1000)

    async def exchange_info(self):
        return await self._req("GET", "/api/v3/exchangeInfo", {"permissions": "SPOT"})

    async def balance(self, asset):
        acc = await self._req("GET", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True)
        for b in acc.get("balances", []):
            if b["asset"] == asset:
                return Decimal(b["free"])
        return Decimal(0)

    async def market_buy_quote(self, symbol, quote_qty: Decimal):
        return await self._req("POST", "/api/v3/order", {
            "symbol": symbol, "side": "BUY", "type": "MARKET",
            "quoteOrderQty": fmt(quote_qty), "newOrderRespType": "FULL"}, signed=True)

    async def limit_sell(self, symbol, qty: Decimal, price: Decimal):
        return await self._req("POST", "/api/v3/order", {
            "symbol": symbol, "side": "SELL", "type": "LIMIT", "timeInForce": "GTC",
            "quantity": fmt(qty), "price": fmt(price)}, signed=True)

    async def market_sell(self, symbol, qty: Decimal):
        return await self._req("POST", "/api/v3/order", {
            "symbol": symbol, "side": "SELL", "type": "MARKET",
            "quantity": fmt(qty), "newOrderRespType": "FULL"}, signed=True)

    async def stop_loss_order(self, symbol, qty: Decimal, stop: Decimal, f):
        """Sell stop on Binance. STOP_LOSS (market) when the pair allows it, else STOP_LOSS_LIMIT 3% lower."""
        params = {"symbol": symbol, "side": "SELL", "quantity": fmt(qty), "stopPrice": fmt(stop)}
        if "STOP_LOSS" in f.get("types", ()):
            params["type"] = "STOP_LOSS"
        else:
            params.update(type="STOP_LOSS_LIMIT", timeInForce="GTC",
                          price=fmt(floor_step(stop * Decimal("0.97"), f["tick"])))
        return await self._req("POST", "/api/v3/order", params, signed=True)

    async def cancel_order(self, symbol, order_id):
        return await self._req("DELETE", "/api/v3/order",
                               {"symbol": symbol, "orderId": order_id}, signed=True)

    async def open_orders(self):
        return await self._req("GET", "/api/v3/openOrders", signed=True)

    async def tickers_24h(self):
        """24h rolling window for every pair (MINI = open/last price only)."""
        return await self._req("GET", "/api/v3/ticker/24hr", {"type": "MINI"})

    async def klines(self, symbol, interval="1m", limit=60):
        return await self._req("GET", "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})

    async def get_order(self, symbol, order_id):
        return await self._req("GET", "/api/v3/order",
                               {"symbol": symbol, "orderId": order_id}, signed=True)


# ─────────────────────────── Telegram ───────────────────────────
def esc(s) -> str:
    return html.escape(str(s), quote=False)


def kb(rows) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


MENU = [
    [("📊 Status", "v:status"), ("💼 Open trades", "v:trades"), ("💰 Balance", "v:balance")],
    [("📈 P&L", "v:pnl"), ("🧾 History", "v:history"), ("📦 Holdings", "v:holdings")],
    [("⏳ Cooldown", "v:cooldown"), ("⏸ Pause entries", "a:pause"), ("▶️ Resume", "a:resume")],
    [("🛑 KILL", "a:kill"), ("💥 Close all (sell everything)", "a:closeall")],
]
VIEW_ONLY_MENU = [[b for b in row if b[1].startswith("v:")] for row in MENU]
VIEW_ONLY_MENU = [r for r in VIEW_ONLY_MENU if r]
VIEW_CMDS = {"/start", "/help", "/menu", "/status", "/trades", "/open", "/balance", "/pnl",
             "/history", "/holdings", "/cooldown"}
COMMANDS = [("menu", "Control panel"), ("status", "Bot status"), ("trades", "Open trades"),
            ("balance", "Balance"), ("pnl", "Profit and loss"), ("history", "Trade history"),
            ("holdings", "Long-term holdings"), ("cooldown", "Coins in the 48h cooldown"),
            ("pause", "Pause new entries"), ("resume", "Resume (clears KILL too)"),
            ("kill", "Stop the bot opening anything"), ("closeall", "Sell every open trade"),
            ("sell", "Sell one trade now: /sell SYMBOL"), ("help", "Help")]
HELP = """<b>Pump bot controls</b>
/menu - control panel buttons
/status /trades /balance /pnl /history /holdings /cooldown
/kill - stop the bot opening anything (stops on Binance stay active)
/resume - clear KILL and pause
/pause - pause new entries only
/closeall - market-sell every open trade (not the long-term holdings)
/sell SYMBOL - sell one trade now
Ladder targets move the stop up automatically; the last target sells."""
VIEW_ONLY_HELP = """<b>Pump bot</b> (view-only chat)
You receive every update. Controls (kill, sell, close all) are disabled in this chat.
/status /trades /balance /pnl /history /holdings /cooldown"""
NOT_ALLOWED = "🔒 View-only chat: controls are disabled here."


def position_buttons(sym: str) -> dict:
    return kb([[("💰 Sell now", f"p:sell:{sym}")]])


EVENTS = collections.deque(maxlen=400)       # notification feed for the dashboard


def add_event(text):
    plain = text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "")
    first = plain[:2]
    level = "critical" if first.startswith(("🔴", "🚨")) and ("fail" in plain.lower() or "error" in plain.lower()) \
        else "warn" if first.startswith("⚠") else "info"
    kind = "trade" if first.startswith(("🟢", "✅", "🔴", "🎯", "🏁", "💰", "📦", "🛑", "🟡", "🟠")) else "system"
    EVENTS.append({"t": time.time(), "level": level, "kind": kind, "text": plain})


class TgBot:
    """One Telegram bot token + chat."""

    def __init__(self, session, token, chat_id, name="main", can_control=True):
        self.s, self.token, self.chat_id = session, token, str(chat_id)
        self.name, self.can_control = name, can_control
        self.base = f"https://api.telegram.org/bot{token}"
        self.q: asyncio.Queue = asyncio.Queue()
        self.username = ""

    async def _call(self, method, payload=None, form=None, timeout=20):
        kw = {"data": form} if form is not None else {"json": payload or {}}
        async with self.s.post(f"{self.base}/{method}", timeout=aiohttp.ClientTimeout(total=timeout), **kw) as r:
            try:
                d = await r.json(content_type=None)
            except Exception:
                d = {"ok": False, "description": f"HTTP {r.status}"}
            d.setdefault("_status", r.status)
            return d

    def _markup(self, markup):
        """View-only chats keep only the view buttons."""
        if self.can_control or not markup:
            return markup
        rows = [[b for b in row if str(b.get("callback_data", "")).startswith("v:")]
                for row in markup.get("inline_keyboard", [])]
        rows = [r for r in rows if r]
        return {"inline_keyboard": rows} if rows else None

    def send(self, text, markup=None):
        self.q.put_nowait(("msg", text, self._markup(markup), None))

    def photo(self, png, caption, markup=None):
        self.q.put_nowait(("photo", caption, self._markup(markup), png))

    async def sender(self):
        while True:
            kind, text, markup, png = await self.q.get()
            try:
                if kind == "photo":
                    form = aiohttp.FormData()
                    form.add_field("chat_id", self.chat_id)
                    form.add_field("caption", text[:1000])
                    form.add_field("parse_mode", "HTML")
                    if markup:
                        form.add_field("reply_markup", json.dumps(markup))
                    form.add_field("photo", png, filename="chart.png", content_type="image/png")
                    d = await self._call("sendPhoto", form=form, timeout=40)
                    if not d.get("ok"):
                        log.warning("[%s] sendPhoto failed (%s) - sending text", self.name, d.get("description"))
                        await self._send_text(text, markup)
                else:
                    await self._send_text(text, markup)
            except Exception as e:
                log.warning("[%s] telegram send failed: %r", self.name, e)
            await asyncio.sleep(1.05)          # stay under Telegram's per-chat limit

    async def _send_text(self, text, markup):
        payload = {"chat_id": self.chat_id, "text": text[:4000], "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if markup:
            payload["reply_markup"] = markup
        for _ in range(3):
            d = await self._call("sendMessage", payload)
            if d.get("ok"):
                return
            if d["_status"] == 429:
                await asyncio.sleep(d.get("parameters", {}).get("retry_after", 5))
                continue
            if d["_status"] == 400 and payload.get("parse_mode"):
                log.warning("[%s] telegram rejected HTML (%s); resending as plain text", self.name, d.get("description"))
                payload.pop("parse_mode")
                payload["text"] = text[:4000].replace("<b>", "").replace("</b>", "") \
                    .replace("<code>", "").replace("</code>", "")
                continue
            log.warning("[%s] telegram send failed: %s", self.name, d.get("description"))
            return

    async def setup(self):
        try:
            me = await self._call("getMe")
            if not me.get("ok"):
                log.error("[%s] Telegram token rejected: %s", self.name, me.get("description"))
                return
            self.username = me["result"].get("username", "")
            await self._call("deleteWebhook", {"drop_pending_updates": False})
            cmds = [{"command": c, "description": d} for c, d in COMMANDS
                    if self.can_control or "/" + c in VIEW_CMDS]
            await self._call("setMyCommands", {"commands": cmds})
            await self._call("setChatMenuButton", {"chat_id": self.chat_id, "menu_button": {"type": "commands"}})
            log.info("[%s] telegram ready as @%s%s", self.name, self.username,
                     "" if self.can_control else " (view-only)")
        except Exception as e:
            log.warning("[%s] telegram setup failed: %r", self.name, e)

    async def poll(self, bot):
        offset, conflicts = 0, 0
        while True:
            try:
                d = await self._call("getUpdates", {"timeout": 30, "offset": offset,
                                                    "allowed_updates": ["message", "callback_query"]}, timeout=45)
                if not d.get("ok"):
                    log.warning("[%s] telegram getUpdates failed: %s %s", self.name, d.get("_status"), d.get("description"))
                    conflicts = conflicts + 1 if d.get("_status") == 409 else 0
                    if conflicts == 3:        # one 409 right after a restart is normal; 3 in a row is not
                        self.send("⚠️ Telegram commands blocked: another program is using this bot token "
                                  "(two copies of the bot running?). Close the other one.")
                    await asyncio.sleep(5)
                    continue
                conflicts = 0
                for u in d.get("result", []):
                    offset = u["update_id"] + 1
                    asyncio.create_task(self._handle(bot, u))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] telegram poll error: %r", self.name, e)
                await asyncio.sleep(5)

    async def _handle(self, bot, u):
        try:
            if "callback_query" in u:
                q = u["callback_query"]
                chat = str(((q.get("message") or {}).get("chat") or {}).get("id"))
                data = q.get("data", "")
                if chat != self.chat_id:
                    return
                if not self.can_control and not data.startswith("v:"):
                    await self._call("answerCallbackQuery", {"callback_query_id": q["id"], "text": NOT_ALLOWED})
                    return
                log.info("[%s] telegram button: %s", self.name, data)
                reply = await bot.dispatch(data, self)
                await self._call("answerCallbackQuery", {"callback_query_id": q["id"], "text": (reply or "")[:190]})
                return
            m = u.get("message") or {}
            chat = str((m.get("chat") or {}).get("id"))
            text = (m.get("text") or "").strip()
            if not text:
                return
            if chat != self.chat_id:
                log.info("[%s] telegram: ignored message from chat %s", self.name, chat)
                return
            parts = text.split()
            cmd = parts[0].split("@")[0].lower()
            if not cmd.startswith("/"):
                cmd = "/" + cmd
            arg = parts[1] if len(parts) > 1 else ""
            log.info("[%s] telegram command: %s %s", self.name, cmd, arg)
            if not self.can_control and cmd not in VIEW_CMDS:
                self.send(NOT_ALLOWED)
                return
            await bot.handle_command(cmd, arg, self)
        except Exception as e:
            log.exception("telegram update failed")
            self.send(f"🔴 Command failed: {esc(e)}")


class Telegram:
    """Sends every message to every configured bot/chat (main bot + TELEGRAM_EXTRA_BOTS).
    Each bot answers commands from its own chat; extra bots can be view-only."""

    def __init__(self, session):
        self.bots = []
        if TG_TOKEN and TG_CHAT_ID:
            self.bots.append(TgBot(session, TG_TOKEN, TG_CHAT_ID, "main", True))
        for i, (tok, chat, ctl) in enumerate(TG_EXTRA_BOTS, 1):
            self.bots.append(TgBot(session, tok, chat, f"extra{i}", ctl))
        self.enabled = bool(self.bots)
        self._last_key = {}

    @property
    def username(self):
        return self.bots[0].username if self.bots else ""

    def send(self, text, key=None, every=0, keyboard=False, markup=None):
        """Queue a message for every bot. `key`+`every` throttle repeated messages (seconds)."""
        if key:
            now = time.time()
            if now - self._last_key.get(key, 0) < every:
                return
            self._last_key[key] = now
        log.info("TG: %s", text.replace("\n", " | "))
        add_event(text)
        if keyboard:
            markup = kb(MENU)
        for b in self.bots:
            b.send(text, markup)

    def photo(self, png, caption, markup=None):
        log.info("TG photo: %s", caption.replace("\n", " | "))
        add_event(caption)
        for b in self.bots:
            b.photo(png, caption, markup)

    async def setup(self):
        await asyncio.gather(*(b.setup() for b in self.bots))

    async def sender(self):
        await asyncio.gather(*(b.sender() for b in self.bots))

    async def poll_commands(self, bot):
        await asyncio.gather(*(b.poll(bot) for b in self.bots))


# ─────────────────────────── Bot ───────────────────────────
class Bot:
    def __init__(self, session):
        self.s = session
        self.api = Binance(session)
        self.tg = Telegram(session)
        self.filters = {}          # symbol -> dict (tradable / scanned pairs)
        self.meta = {}             # symbol -> dict (all USDT pairs, used for exits)
        self.streaming = set()
        self.positions = {}        # symbol -> position dict
        self.holdings = {}         # symbol -> long-term holding (TP cancelled after big drop)
        self.pending = set()
        self.exiting = set()       # symbols with a cancel / stop-loss in progress
        self.last_buy = {}         # symbol -> unix time of last buy (cooldown)
        self.last_price = {}
        self.paused = False
        self.killed = False        # KILL switch: no new buys, survives restarts
        self.history = []          # last closed trades (for /history and /pnl)
        self.signals = collections.deque(maxlen=80)   # recent pump signals for the dashboard
        self._sig_seen = collections.OrderedDict()
        self.free_usdt = None      # cached for the dashboard
        self.open24 = {}           # symbol -> 24h open price (Binance gainers / losers list)
        self.open24_at = 0
        self.stats = {"buys": 0, "tp_hits": 0, "sl_hits": 0, "held": 0, "realized": "0"}
        self.started = time.time()
        self.load_state()

    # ── state ──
    def load_state(self):
        self.startup_notes = []
        try:
            with open(STATE_FILE) as f:
                d = json.load(f)
            self.positions = d.get("positions", {})
            self.holdings = d.get("holdings", {})
            self.killed = bool(d.get("killed", False))
            self.history = d.get("history", [])[-200:]
            self.stats.update(d.get("stats", {}))
            self.last_buy = {k: float(v) for k, v in d.get("last_buy", {}).items()}
        except FileNotFoundError:
            pass
        except Exception as e:
            log.error("state load failed: %s", e)
            return
        self.apply_cleanup()
        if not DRY_RUN:                 # safety: a live bot never keeps paper trades
            for book in (self.positions, self.holdings):
                for sym in [k for k, p in book.items() if p.get("dry")]:
                    book.pop(sym)
                    self.startup_notes.append(f"removed paper trade {sym}")
            before = len(self.history)
            self.history = [h for h in self.history if not h.get("dry")]
            if len(self.history) != before:
                self.startup_notes.append(f"removed {before - len(self.history)} paper sells from history")
        # older state files have no last_buy: seed the cooldown from open trades
        for sym, p in list(self.positions.items()) + list(self.holdings.items()):
            self.last_buy[sym] = max(self.last_buy.get(sym, 0), float(p.get("opened_at", 0)))
        log.info("loaded %d open positions, %d holdings from %s",
                 len(self.positions), len(self.holdings), STATE_FILE)
        if self.startup_notes:
            self.save_state()

    def apply_cleanup(self):
        """One-time fix-up file (STATE_FILE.cleanup.json), renamed to .applied.json once used."""
        path = STATE_FILE + ".cleanup.json"
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                c = json.load(f)
            t0, t1 = c.get("paper_window", [0, 0])
            for sym in c.get("remove_positions", []):
                p = self.positions.get(sym)
                if p and t0 <= p.get("opened_at", 0) <= t1:
                    self.positions.pop(sym)
                    self.last_buy.pop(sym, None)
                    self.startup_notes.append(f"removed paper trade {sym}")
            gone = [h for h in self.history if t0 <= h["t"] <= t1]
            if gone:
                self.history = [h for h in self.history if not (t0 <= h["t"] <= t1)]
                sells = [h for h in gone if h.get("kind") != "hold"]
                pnl = sum((Decimal(h["pnl"]) for h in sells), Decimal(0))
                self.stats["realized"] = fmt((Decimal(self.stats["realized"]) - pnl).quantize(Decimal("0.000001")))
                self.stats["tp_hits"] = max(0, self.stats.get("tp_hits", 0) - sum(1 for h in sells if Decimal(h["pnl"]) >= 0))
                self.stats["sl_hits"] = max(0, self.stats.get("sl_hits", 0) - sum(1 for h in sells if Decimal(h["pnl"]) < 0))
                self.startup_notes.append(f"removed {len(gone)} paper sells from P&L ({pnl:+.4f} USDT)")
            if c.get("paper_buys"):
                self.stats["buys"] = max(0, self.stats.get("buys", 0) - int(c["paper_buys"]))
            bf = c.get("backfill_ladder")
            if bf:
                for sym, p in self.positions.items():
                    if p.get("mode") == "ladder" and not p.get("targets"):
                        p["targets"] = bf["targets"]
                        p["first_stop_pct"] = bf["first_stop_pct"]
                        p["stop_mode"] = bf["stop_mode"]
                        self.startup_notes.append(f"{sym} keeps its old ladder (T1 +{bf['targets'][0]}%, "
                                                  f"stop after T1 +{bf['first_stop_pct']}%)")
            for sym, p in c.get("restore_positions", {}).items():
                if sym not in self.positions:
                    self.positions[sym] = p
                    self.startup_notes.append(f"restored real trade {sym} (its sell was only simulated)")
            os.replace(path, STATE_FILE + ".cleanup.applied.json")
            log.info("cleanup applied: %s", "; ".join(self.startup_notes))
        except Exception as e:
            log.error("cleanup file not applied: %s", e)

    def save_state(self):
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            now = time.time()
            cooldowns = {k: int(v) for k, v in self.last_buy.items()
                         if now - v < SYMBOL_COOLDOWN_SEC}
            json.dump({"positions": self.positions, "holdings": self.holdings,
                       "last_buy": cooldowns, "stats": self.stats,
                       "killed": self.killed, "history": self.history[-200:]}, f, indent=2)
        os.replace(tmp, STATE_FILE)

    # ── symbols ──
    async def load_symbols(self):
        info = await self.api.exchange_info()
        fresh, meta, excluded = {}, {}, []
        for s in info["symbols"]:
            if s.get("status") != "TRADING" or s.get("quoteAsset") != QUOTE:
                continue
            if not s.get("isSpotTradingAllowed", True):
                continue
            sym, base = s["symbol"], s["baseAsset"]
            f = {"base": base, "tick": Decimal("0"), "step": Decimal("0"),
                 "min_qty": Decimal("0"), "min_notional": Decimal("0"),
                 "types": set(s.get("orderTypes", []))}
            for flt in s.get("filters", []):
                t = flt.get("filterType")
                if t == "PRICE_FILTER":
                    f["tick"] = Decimal(flt["tickSize"])
                elif t == "LOT_SIZE":
                    f["step"] = Decimal(flt["stepSize"])
                    f["min_qty"] = Decimal(flt["minQty"])
                elif t in ("NOTIONAL", "MIN_NOTIONAL"):
                    f["min_notional"] = Decimal(flt.get("minNotional", "0"))
            meta[sym] = f
            if is_excluded(sym, base):
                excluded.append(sym)
                continue
            fresh[sym] = f
        self.filters = fresh
        self.meta = meta
        # keep price streams for open trades even if their pair is now excluded,
        # so the drawdown-hold / stop-loss checks still see the price
        watch = set(fresh) | {s for s in self.positions if s in meta}
        return sorted(watch), sorted(excluded)

    # ── streams ──
    async def stream_worker(self, symbols):
        streams = "/".join(f"{s.lower()}@kline_1m/{s.lower()}@kline_1s" for s in symbols)
        url = WS + streams
        backoff = 1
        while True:
            try:
                async with self.s.ws_connect(url, heartbeat=20, max_msg_size=0) as ws:
                    log.info("ws connected (%d symbols, e.g. %s)", len(symbols), symbols[0])
                    backoff = 1
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            d = json.loads(msg.data).get("data") or {}
                            if d.get("e") == "kline":
                                self.on_kline(d["s"], d["k"])
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except Exception as e:
                log.warning("ws error: %s", e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def start_streams(self, symbols):
        new = [s for s in symbols if s not in self.streaming]
        for i in range(0, len(new), SYMBOLS_PER_SOCKET):
            chunk = new[i:i + SYMBOLS_PER_SOCKET]
            self.streaming.update(chunk)
            asyncio.create_task(self.stream_worker(chunk))
        return len(new)

    # ── signal ──
    def change24(self, sym, price):
        """24h % change using the live price (same number as Binance's gainers / losers list)."""
        o = self.open24.get(sym)
        return (price / o - 1) * 100 if o and price else None

    async def refresh_24h(self):
        rows = await self.api.tickers_24h()
        self.open24 = {r["symbol"]: float(r["openPrice"]) for r in rows
                       if r.get("symbol", "").endswith(QUOTE) and float(r.get("openPrice", 0) or 0) > 0}
        self.open24_at = time.time()

    async def ticker_loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self.refresh_24h()
            except Exception as e:
                log.warning("24h ticker refresh failed: %s", e)

    def on_kline(self, sym, k):
        try:
            o, c = float(k["o"]), float(k["c"])
        except (KeyError, ValueError):
            return
        self.last_price[sym] = c
        if sym in self.positions:
            self.check_exit(sym, c)
        if o <= 0:
            return
        iv = k.get("i")
        chg = (c - o) / o * 100
        if iv == "1s":
            if chg < PUMP_1S_PCT or float(k.get("q", 0)) < MIN_QUOTE_VOL_1S:
                return
        elif iv == "1m":
            if chg < PUMP_1M_PCT or float(k.get("q", 0)) < MIN_QUOTE_VOL_1M:
                return
        else:
            return
        if sym not in self.filters:
            return
        if self.killed:
            return self._signal(sym, iv, chg, c, k, "skipped: KILL on")
        if self.paused:
            return self._signal(sym, iv, chg, c, k, "skipped: paused")
        if sym in self.positions or sym in self.pending:
            return self._signal(sym, iv, chg, c, k, "already open")
        if time.time() - self.last_buy.get(sym, 0) < SYMBOL_COOLDOWN_SEC:
            return self._signal(sym, iv, chg, c, k, "skipped: cooldown")
        if SKIP_HELD_COINS and sym in self.holdings:
            return self._signal(sym, iv, chg, c, k, "skipped: in holdings")
        if SKIP_24H_LOSERS:
            ch24 = self.change24(sym, c)
            if ch24 is None or time.time() - self.open24_at > 300:
                return self._signal(sym, iv, chg, c, k, "skipped: no 24h data")
            if ch24 < MIN_24H_CHANGE_PCT:
                return self._signal(sym, iv, chg, c, k, f"skipped: 24h loser ({ch24:+.2f}%)")
        if len(self.positions) + len(self.pending) >= MAX_OPEN_TRADES:
            self.tg.send(f"⚠️ Max open trades ({MAX_OPEN_TRADES}) reached — skipping signals.",
                         key="maxopen", every=900)
            return self._signal(sym, iv, chg, c, k, "skipped: max open trades")
        self._signal(sym, iv, chg, c, k, "BUY")
        self.pending.add(sym)
        self.last_buy[sym] = time.time()
        asyncio.create_task(self.buy(sym, iv, chg, c))

    def _signal(self, sym, iv, chg, price, k, result):
        """Remember a pump signal once per candle (the stream repeats the same candle)."""
        key = (sym, iv, k.get("t"))
        if key in self._sig_seen:
            return
        self._sig_seen[key] = 1
        if len(self._sig_seen) > 2000:
            self._sig_seen.popitem(last=False)
        ch24 = self.change24(sym, price)
        self.signals.append({"t": time.time(), "sym": sym, "iv": iv, "chg": round(chg, 2),
                             "ch24": round(ch24, 2) if ch24 is not None else None,
                             "price": price, "result": result})

    # ── trading ──
    async def buy(self, sym, iv, chg, hint_price):
        f = self.filters[sym]
        try:
            if DRY_RUN:
                price = Decimal(str(hint_price))
                qty = floor_step(TRADE_USDT / price, f["step"])
                cost = qty * price
            else:
                r = await self.api.market_buy_quote(sym, TRADE_USDT)
                exec_qty = Decimal(r["executedQty"])
                cost = Decimal(r["cummulativeQuoteQty"])
                if exec_qty <= 0:
                    raise RuntimeError(f"market buy returned 0 qty (status {r.get('status')})")
                fee_base = sum((Decimal(x["commission"]) for x in r.get("fills", [])
                                if x["commissionAsset"] == f["base"]), Decimal(0))
                qty = exec_qty - fee_base
                price = cost / exec_qty

            sell_qty = floor_step(qty, f["step"])
            self.stats["buys"] += 1
            if EXIT_MODE == "ladder":
                ch24 = self.change24(sym, float(price))
                await self._open_ladder(sym, f, sell_qty, price, cost, f"{iv} +{chg:.2f}%"
                                        + (f" · 24h {ch24:+.1f}%" if ch24 is not None else ""))
                return

            tp = ceil_step(price * (1 + TAKE_PROFIT_PCT / 100), f["tick"])
            pos = {"symbol": sym, "mode": "tp", "qty": fmt(sell_qty), "buy_price": fmt(price),
                   "cost": fmt(cost), "tp_price": fmt(tp), "order_id": None,
                   "trigger": f"{iv} +{chg:.2f}%", "opened_at": int(time.time()), "dry": DRY_RUN}

            if sell_qty < f["min_qty"] or sell_qty * tp < f["min_notional"]:
                self.positions[sym] = pos
                self.save_state()
                self.tg.send(
                    f"🟡 <b>BUY {sym}</b> ({pos['trigger']})\n"
                    f"Qty {fmt(sell_qty)} @ {fmt(price)} = {fmt(cost.quantize(Decimal('0.01')))} USDT\n"
                    f"⚠️ TP not placed: {fmt(sell_qty * tp)} USDT is below min notional "
                    f"{fmt(f['min_notional'])}. Raise TRADE_USDT (6 is safer).")
                return

            self.positions[sym] = pos          # record the bag before placing the TP
            self.save_state()
            if DRY_RUN:
                pos["order_id"] = "DRY"
            else:
                try:
                    o = await self.api.limit_sell(sym, sell_qty, tp)
                except BinanceError as e:
                    self.tg.send(
                        f"🟠 <b>BUY {sym}</b> filled ({pos['qty']} @ {pos['buy_price']}) "
                        f"but TP order FAILED: {e.msg} (code {e.code}). Sell it manually.")
                    return
                pos["order_id"] = o["orderId"]
            self.save_state()
            self.snapshot(sym, pos, "BUY", caption=
                f"🟢 <b>BUY {sym}</b>{' [DRY]' if DRY_RUN else ''}\n"
                f"Trigger: {pos['trigger']}\n"
                f"Qty {pos['qty']} @ {pos['buy_price']} = {fmt(cost.quantize(Decimal('0.01')))} USDT\n"
                f"🎯 TP sell placed @ {pos['tp_price']} (+{fmt(TAKE_PROFIT_PCT)}%)\n"
                f"Open trades: {len(self.positions)}")
        except BinanceError as e:
            if e.code == -2010 and "insufficient" in (e.msg or "").lower():
                self.tg.send("🔴 Insufficient USDT balance — buys are failing.",
                             key="nobal", every=900)
            else:
                self.tg.send(f"🔴 Buy error {sym}: {e.msg} (code {e.code})",
                             key=f"err{sym}", every=60)
        except Exception as e:
            log.exception("buy failed")
            self.tg.send(f"🔴 Buy failed {sym}: {e}", key=f"err{sym}", every=60)
        finally:
            self.pending.discard(sym)
            if sym not in self.positions:
                # buy didn't happen: don't block the coin for 48h, just 5 minutes
                self.last_buy[sym] = time.time() - SYMBOL_COOLDOWN_SEC + 300

    # ── ladder stop-loss (from the VAMSI bot) ──
    # Each trade keeps the ladder it was bought with (so .env changes only affect new trades)
    @staticmethod
    def p_targets(p):
        return [Decimal(str(x)) for x in p["targets"]] if p.get("targets") else LADDER_TARGETS

    @staticmethod
    def p_first_stop(p):
        return Decimal(str(p["first_stop_pct"])) if p.get("first_stop_pct") is not None else LADDER_FIRST_STOP_PCT

    @staticmethod
    def p_stop_mode(p):
        return p.get("stop_mode") or LADDER_STOP_MODE

    def target_price(self, p, k):
        """Price of ladder target k (1-based), from the actual average buy price."""
        return Decimal(p["buy_price"]) * (1 + self.p_targets(p)[k - 1] / 100)

    def targets_reached(self, p, price: Decimal):
        return sum(1 for k in range(1, len(self.p_targets(p)) + 1) if price >= self.target_price(p, k))

    def ladder_stop_for(self, p, k):
        """Stop price + label once target k (1-based) has been reached."""
        T, first = self.p_targets(p), self.p_first_stop(p)
        if self.p_stop_mode(p) == "same":
            return self.target_price(p, k), f"+{fmt(T[k - 1])}% (T{k})"
        if k == 1:
            return (Decimal(p["buy_price"]) * (1 + first / 100), f"+{fmt(first)}% (after T1)")
        return self.target_price(p, k - 1), f"+{fmt(T[k - 2])}% (T{k - 1}, after T{k})"

    async def _open_ladder(self, sym, f, qty, price, cost, trigger):
        stop = floor_step(price * (1 - STOP_LOSS_PCT / 100), f["tick"]) \
            if STOP_LOSS_ENABLED and STOP_LOSS_PCT > 0 else None
        pos = {"symbol": sym, "mode": "ladder", "qty": fmt(qty), "buy_price": fmt(price),
               "cost": fmt(cost), "tp_price": fmt(ceil_step(price * (1 + LADDER_TARGETS[0] / 100), f["tick"])),
               "targets_hit": 0, "stop_price": fmt(stop) if stop else None,
               "stop_label": f"-{fmt(STOP_LOSS_PCT)}%" if stop else None,
               "order_id": None, "stop_order_price": None,
               "targets": [fmt(t) for t in LADDER_TARGETS], "first_stop_pct": fmt(LADDER_FIRST_STOP_PCT),
               "stop_mode": LADDER_STOP_MODE,
               "trigger": trigger, "opened_at": int(time.time()), "dry": DRY_RUN}
        self.exiting.add(sym)                  # no exit checks until the stop order is in place
        try:
            self.positions[sym] = pos
            self.save_state()
            warn = ""
            if qty < f["min_qty"] or qty * price < f["min_notional"]:
                warn = (f"\n⚠️ {fmt(qty * price)} USDT is below Binance's min order "
                        f"{fmt(f['min_notional'])} — the bot may not be able to sell it. Raise TRADE_USDT.")
            elif stop and await self.sync_stop(sym, pos) == "sell":
                warn = "\n⚠️ Price already below the stop — selling."
            ladder = " / ".join(f"+{fmt(t)}%" for t in LADDER_TARGETS[:6]) + (" …" if len(LADDER_TARGETS) > 6 else "")
            self.snapshot(sym, pos, "BUY", caption=
                f"🟢 <b>BUY {sym}</b>{' [DRY]' if DRY_RUN else ''}\n"
                f"Trigger: {trigger}\n"
                f"Qty {pos['qty']} @ {pos['buy_price']} = {fmt(cost.quantize(Decimal('0.01')))} USDT\n"
                f"🪜 Ladder targets {ladder}"
                f"{' (last sells 100%)' if LADDER_SELL_AT_LAST else ''}\n"
                f"Stop: {('-' + fmt(STOP_LOSS_PCT) + '% @ ' + pos['stop_price']) if stop else 'none until Target 1'}"
                f"{' (on Binance)' if pos.get('order_id') else ''}"
                f"{'' if stop or HOLD_DRAWDOWN_PCT <= 0 else ' · hold at -' + fmt(HOLD_DRAWDOWN_PCT) + '%'}\n"
                f"Open trades: {len(self.positions)}{warn}")
        finally:
            self.exiting.discard(sym)
        lp = self.last_price.get(sym)
        if lp:
            self.check_exit(sym, lp)

    async def sync_stop(self, sym, p):
        """Keep a real STOP_LOSS order on Binance at the ladder stop.
        Returns 'ok', 'filled' (the old stop already sold) or 'sell' (price is already below the stop)."""
        if DRY_RUN or not LADDER_EXCHANGE_STOP or not p.get("stop_price"):
            return "ok"
        if p.get("order_id") and p.get("stop_order_price") == p["stop_price"]:
            return "ok"
        if p.get("order_id"):
            if await self._cancel_order(sym, p) == "filled":
                return "filled"
            p["order_id"], p["stop_order_price"] = None, None
        f = self.meta.get(sym) or self.filters.get(sym)
        qty, stop = Decimal(p["qty"]), Decimal(p["stop_price"])
        if not f or qty < f["min_qty"] or qty * stop < f["min_notional"]:
            log.info("%s: stop order not placed (below min notional); bot-side stop only", sym)
            self.save_state()
            return "ok"
        try:
            o = await self.api.stop_loss_order(sym, qty, floor_step(stop, f["tick"]), f)
        except BinanceError as e:
            if e.code == -2010 and "immediately" in (e.msg or "").lower():
                return "sell"
            self.save_state()
            self.tg.send(f"⚠️ {sym}: Binance stop order not placed ({e.msg}, code {e.code}). "
                         f"Bot-side stop still active.", key=f"stop{sym}", every=600)
            return "ok"
        p["order_id"], p["stop_order_price"] = o["orderId"], p["stop_price"]
        self.save_state()
        return "ok"

    async def ladder_targets(self, sym, p, price: Decimal):
        T = self.p_targets(p)
        k = self.targets_reached(p, price)
        if k <= p.get("targets_hit", 0):
            return
        p["targets_hit"] = k
        pct = T[k - 1]
        if k == len(T) and LADDER_SELL_AT_LAST:
            self.save_state()
            if await self._cancel_order(sym, p) == "filled":
                return
            if not await self.market_exit(sym, p, "🏁", f"TARGET {k} (+{fmt(pct)}%) — SOLD 100%"):
                self.move_to_holdings(sym, p, Decimal(0))
            return
        f = self.meta.get(sym) or self.filters.get(sym) or {"tick": Decimal(0)}
        new_stop, label = self.ladder_stop_for(p, k)
        new_stop = floor_step(new_stop, f["tick"])
        if not p.get("stop_price") or new_stop > Decimal(p["stop_price"]):      # only ever up
            p["stop_price"], p["stop_label"] = fmt(new_stop), label
        nxt = ""
        if k < len(T):
            p["tp_price"] = fmt(ceil_step(self.target_price(p, k + 1), f["tick"]))
            nxt = f"\nNext: Target {k + 1} (+{fmt(T[k])}%) @ {p['tp_price']}"
        self.save_state()
        r = await self.sync_stop(sym, p)
        if r == "filled":
            return
        self.tg.send(
            f"🎯 <b>{sym} Target {k} (+{fmt(pct)}%)</b>{' [DRY]' if DRY_RUN else ''} reached @ {fmt(price)}\n"
            f"Not selling — stop moved up to {p['stop_label']} @ {p['stop_price']}"
            f"{' (on Binance)' if p.get('order_id') else ''}{nxt}")
        if r == "sell":
            if not await self.market_exit(sym, p, "🔴", f"LADDER STOP {p['stop_label']}"):
                self.move_to_holdings(sym, p, Decimal(0))

    # ── drawdown hold / stop-loss ──
    def check_exit(self, sym, price):
        if sym in self.exiting:
            return
        p = self.positions.get(sym)
        if not p:
            return
        buy = Decimal(p["buy_price"])
        if buy <= 0:
            return
        px = Decimal(str(price))
        drop = (buy - px) / buy * 100      # % below buy price
        if p.get("mode") == "ladder":
            stop = p.get("stop_price")
            if stop and px <= Decimal(stop):
                action = "ladder_sell"
            elif self.targets_reached(p, px) > p.get("targets_hit", 0):
                action = "target"
            elif not stop and HOLD_DRAWDOWN_PCT > 0 and drop >= HOLD_DRAWDOWN_PCT:
                action = "hold"
            else:
                return
        elif STOP_LOSS_ENABLED and STOP_LOSS_PCT > 0 and drop >= STOP_LOSS_PCT:
            action = "stoploss"
        elif HOLD_DRAWDOWN_PCT > 0 and drop >= HOLD_DRAWDOWN_PCT:
            action = "hold"
        else:
            return
        self.exiting.add(sym)
        asyncio.create_task(self.exit_position(sym, action, drop, px))

    async def _cancel_order(self, sym, p):
        """Cancel the position's sell order (TP limit or ladder stop).
        Returns 'ok', 'filled' (it already sold — recorded) or raises."""
        oid = p.get("order_id")
        if DRY_RUN or oid in (None, "DRY"):
            return "ok"
        try:
            await self.api.cancel_order(sym, oid)
            p["order_id"] = None
            p["stop_order_price"] = None
            return "ok"
        except BinanceError as e:
            if e.code != -2011:          # -2011 = unknown order / already closed
                raise
            o = await self.api.get_order(sym, oid)
            if o.get("status") in ("FILLED", "PARTIALLY_FILLED") and Decimal(o.get("executedQty", "0")) > 0:
                self.record_fill(sym, p, Decimal(o["cummulativeQuoteQty"]))
                return "filled"
            p["order_id"] = None
            p["stop_order_price"] = None
            return "ok"

    def record_fill(self, sym, p, proceeds: Decimal):
        """The sell order on Binance filled by itself."""
        if p.get("mode") == "ladder":
            self.finish_sell(sym, p, proceeds, "🔴", f"BINANCE STOP FILLED ({p.get('stop_label')})",
                             f"Sold {p['qty']} @ ~{p.get('stop_price')}")
        else:
            self.close(sym, proceeds)

    async def exit_position(self, sym, action, drop, px=None):
        try:
            p = self.positions.get(sym)
            if not p:
                return
            if action == "target":
                await self.ladder_targets(sym, p, px)
                return
            if await self._cancel_order(sym, p) == "filled":
                return
            if action == "ladder_sell":
                title = f"LADDER STOP {p.get('stop_label')}"
                if await self.market_exit(sym, p, "🔴" if drop > 0 else "🟢", title):
                    return
            elif action == "stoploss":
                if await self.market_exit(sym, p, "🛑", "STOP-LOSS"):
                    return
            self.move_to_holdings(sym, p, drop)  # hold, or market sell impossible → keep the coin
        except Exception as e:
            log.exception("exit failed")
            self.tg.send(f"🔴 Exit ({action}) failed for {sym}: {e}", key=f"exit{sym}", every=120)
        finally:
            self.exiting.discard(sym)

    async def market_exit(self, sym, p, emoji, title):
        """Market-sell the whole position. False if Binance won't accept it (below minimum)."""
        f = self.meta.get(sym) or self.filters.get(sym)
        qty = Decimal(p["qty"])
        lp = Decimal(str(self.last_price.get(sym, p["buy_price"])))
        if f:
            qty = floor_step(qty, f["step"])
            if qty < f["min_qty"] or qty * lp < f["min_notional"]:
                self.tg.send(f"⚠️ {title} {sym}: value {fmt(qty * lp)} USDT is below Binance "
                             f"minimum — keeping it as a holding instead.")
                return False
        if DRY_RUN:
            proceeds = qty * lp
        else:
            r = await self.api.market_sell(sym, qty)
            proceeds = Decimal(r["cummulativeQuoteQty"])
            if Decimal(r.get("executedQty", "0")) <= 0:
                raise RuntimeError(f"market sell returned 0 qty (status {r.get('status')})")
            lp = proceeds / Decimal(r["executedQty"])
        self.finish_sell(sym, p, proceeds, emoji, title, f"Sold {fmt(qty)} @ ~{lp:.8g}")
        return True

    def finish_sell(self, sym, p, proceeds: Decimal, emoji, title, detail):
        self.positions.pop(sym, None)
        pnl = proceeds - Decimal(p["cost"])
        key = "tp_hits" if pnl >= 0 else "sl_hits"
        self.stats[key] = self.stats.get(key, 0) + 1
        self.stats["realized"] = fmt((Decimal(self.stats["realized"]) + pnl).quantize(Decimal("0.000001")))
        self.save_state()
        held = int(time.time()) - p["opened_at"]
        pct = (proceeds / Decimal(p["cost"]) - 1) * 100 if Decimal(p["cost"]) > 0 else Decimal(0)
        self.record_history(sym, pnl, pct, title)
        qty = Decimal(p["qty"])
        exit_px = proceeds / qty if qty > 0 else None
        self.snapshot(sym, p, "SELL", exit_price=exit_px, caption=
            f"{emoji} <b>{esc(title)} {sym}</b>{' [DRY]' if DRY_RUN else ''}\n"
            f"{detail} · buy {p['buy_price']}\n"
            f"P/L ≈ {pnl:+.4f} USDT ({pct:+.2f}%, before sell fee)\n"
            f"Held {held // 3600}h {held % 3600 // 60}m · Total realized {Decimal(self.stats['realized']):+.4f}")

    def record_history(self, sym, pnl, pct, reason):
        self.history.append({"t": int(time.time()), "sym": sym, "pnl": fmt(Decimal(pnl).quantize(Decimal("0.000001"))),
                             "pct": f"{float(pct):.2f}", "reason": str(reason), "dry": DRY_RUN})
        self.history = self.history[-200:]
        self.save_state()

    def move_to_holdings(self, sym, p, drop):
        self.positions.pop(sym, None)
        h = dict(p)
        h["order_id"] = None
        h["held_at"] = int(time.time())
        h["held_drop_pct"] = f"{drop:.2f}"
        if sym in self.holdings:             # merge with an earlier holding of the same coin
            old = self.holdings[sym]
            q = Decimal(old["qty"]) + Decimal(h["qty"])
            c = Decimal(old["cost"]) + Decimal(h["cost"])
            h["qty"], h["cost"] = fmt(q), fmt(c)
            h["buy_price"] = fmt(c / q) if q > 0 else h["buy_price"]
        self.holdings[sym] = h
        self.stats["held"] = self.stats.get("held", 0) + 1
        self.history.append({"t": int(time.time()), "sym": sym, "pnl": "0", "pct": f"{-drop:.2f}",
                             "reason": "moved to long-term hold", "kind": "hold", "dry": DRY_RUN})
        self.save_state()
        self.tg.send(
            f"📦 <b>HOLD {sym}</b>{' [DRY]' if DRY_RUN else ''}\n"
            f"Down {drop:.2f}% from buy {p['buy_price']} — sell order cancelled.\n"
            f"Kept {p['qty']} {sym[:-len(QUOTE)]} as a long-term holding (bot will not sell it).\n"
            f"Slot freed · Open trades {len(self.positions)}/{MAX_OPEN_TRADES}")

    async def _resync(self, sym):
        try:
            p = self.positions.get(sym)
            if p and await self.sync_stop(sym, p) == "sell":
                if not await self.market_exit(sym, p, "🔴", f"LADDER STOP {p.get('stop_label')}"):
                    self.move_to_holdings(sym, p, Decimal(0))
        except Exception as e:
            log.warning("stop resync %s failed: %s", sym, e)
        finally:
            self.exiting.discard(sym)

    async def monitor(self):
        while True:
            await asyncio.sleep(POLL_SEC)
            try:
                for sym in list(self.positions):          # safety net for the price checks
                    lp = self.last_price.get(sym)
                    if lp:
                        self.check_exit(sym, lp)
                if DRY_RUN:
                    for sym, p in list(self.positions.items()):
                        if sym in self.exiting or p.get("mode") == "ladder":
                            continue
                        lp = self.last_price.get(sym)
                        if p.get("order_id") == "DRY" and lp and Decimal(str(lp)) >= Decimal(p["tp_price"]):
                            proceeds = Decimal(p["qty"]) * Decimal(p["tp_price"])
                            self.close(sym, proceeds)
                    continue

                live = {str(o["orderId"]) for o in await self.api.open_orders()}
                for sym, p in list(self.positions.items()):
                    ladder = p.get("mode") == "ladder"
                    if ladder and LADDER_EXCHANGE_STOP and p.get("stop_price") and not p.get("order_id") \
                            and sym not in self.exiting:
                        self.exiting.add(sym)                # stop order missing: try to place it again
                        asyncio.create_task(self._resync(sym))
                        continue
                    oid = p.get("order_id")
                    if oid is None or str(oid) in live or sym in self.exiting:
                        continue
                    o = await self.api.get_order(sym, oid)
                    if sym in self.exiting or self.positions.get(sym) is not p:
                        continue                             # our own cancel is in progress
                    st = o.get("status")
                    if st == "FILLED":
                        self.record_fill(sym, p, Decimal(o["cummulativeQuoteQty"]))
                    elif st in ("CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"):
                        if ladder:                           # bot-side stop keeps working; re-place it
                            p["order_id"], p["stop_order_price"] = None, None
                            self.save_state()
                            self.tg.send(f"⚠️ Binance stop order for {sym} was {st}. Re-placing it "
                                         f"(bot-side stop still active).", key=f"stopx{sym}", every=600)
                            continue
                        self.positions.pop(sym, None)
                        self.save_state()
                        self.tg.send(f"⚠️ TP order for {sym} was {st}. "
                                     f"Position no longer tracked — check it manually.")
            except Exception as e:
                log.warning("monitor error: %s", e)

    def close(self, sym, proceeds: Decimal):
        p = self.positions.pop(sym, None)
        if p is None:
            return
        pnl = proceeds - Decimal(p["cost"])
        self.stats["tp_hits"] += 1
        self.stats["realized"] = fmt((Decimal(self.stats["realized"]) + pnl).quantize(Decimal("0.000001")))
        self.save_state()
        held = int(time.time()) - p["opened_at"]
        self.record_history(sym, pnl, (proceeds / Decimal(p["cost"]) - 1) * 100 if Decimal(p["cost"]) > 0 else 0,
                            "TP hit")
        self.snapshot(sym, p, "SELL", exit_price=Decimal(p["tp_price"]), caption=
            f"✅ <b>TP HIT {sym}</b>{' [DRY]' if DRY_RUN else ''}\n"
            f"Sold {p['qty']} @ {p['tp_price']}\n"
            f"Profit ≈ {pnl:+.4f} USDT (gross, before sell fee)\n"
            f"Held {held // 3600}h {held % 3600 // 60}m · Total realized {Decimal(self.stats['realized']):+.4f}")

    async def refresh_symbols(self):
        while True:
            await asyncio.sleep(EXINFO_REFRESH_SEC)
            try:
                await self.api.sync_time()
                syms, _ = await self.load_symbols()
                n = self.start_streams(syms)
                if n:
                    self.tg.send(f"ℹ️ {n} new pair(s) added to the scanner.")
            except Exception as e:
                log.warning("refresh error: %s", e)

    # ── telegram: views ──
    def _pct_now(self, sym, buy):
        lp = self.last_price.get(sym)
        return (lp / float(buy) - 1) * 100 if lp and float(buy) > 0 else None

    def _today_start(self):
        lt = time.localtime()
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))

    def _unrealized(self, items):
        tot = Decimal(0)
        for sym, p in items:
            lp = self.last_price.get(sym)
            if lp:
                tot += Decimal(p["qty"]) * Decimal(str(lp)) - Decimal(p["cost"])
        return tot

    async def text_for(self, view):
        if view == "status":
            up = int(time.time() - self.started)
            mode = "🛑 KILLED" if self.killed else "⏸ PAUSED" if self.paused else "▶️ running"
            exits = (f"🪜 Ladder {' / '.join('+' + fmt(t) + '%' for t in LADDER_TARGETS)}"
                     if EXIT_MODE == "ladder" else f"TP +{fmt(TAKE_PROFIT_PCT)}%")
            return (f"📊 <b>Status</b> {'[DRY RUN]' if DRY_RUN else '[LIVE]'} · {mode}\n"
                    f"Uptime {up // 3600}h {up % 3600 // 60}m · Scanning {len(self.streaming)} pairs\n"
                    f"Exits: {exits}\n"
                    f"Buys {self.stats['buys']} · wins {self.stats['tp_hits']} · losses {self.stats.get('sl_hits', 0)} · "
                    f"moved to hold {self.stats.get('held', 0)}\n"
                    f"Open {len(self.positions)}/{MAX_OPEN_TRADES} · Holdings {len(self.holdings)} · "
                    f"Cooldown {sum(1 for t in self.last_buy.values() if time.time() - t < SYMBOL_COOLDOWN_SEC)}\n"
                    f"Realized {Decimal(self.stats['realized']):+.4f} USDT")
        if view == "balance":
            lines = ["💰 <b>Balance</b>"]
            if DRY_RUN:
                lines.append("DRY RUN - no real balance")
            else:
                try:
                    lines.append(f"Free USDT: {fmt((await self.api.balance(QUOTE)).quantize(Decimal('0.01')))}")
                except Exception as e:
                    lines.append(f"Free USDT: n/a ({esc(e)})")
            def val(items):
                return sum((Decimal(p["qty"]) * Decimal(str(self.last_price[s])) for s, p in items
                            if self.last_price.get(s)), Decimal(0))
            cost_o = sum((Decimal(p["cost"]) for p in self.positions.values()), Decimal(0))
            cost_h = sum((Decimal(p["cost"]) for p in self.holdings.values()), Decimal(0))
            lines.append(f"Open trades: {len(self.positions)} · cost {cost_o:.2f} · now ≈ {val(self.positions.items()):.2f} USDT")
            lines.append(f"Long-term holdings: {len(self.holdings)} · cost {cost_h:.2f} · now ≈ "
                         f"{val(self.holdings.items()):.2f} USDT (prices only for scanned pairs)")
            return "\n".join(lines)
        if view == "pnl":
            t0 = self._today_start()
            today = [h for h in self.history if h["t"] >= t0]
            pnl_today = sum((Decimal(h["pnl"]) for h in today), Decimal(0))
            wins = sum(1 for h in today if Decimal(h["pnl"]) >= 0)
            return (f"📈 <b>P/L</b>\n"
                    f"Today: {pnl_today:+.4f} USDT ({len(today)} sells, {wins} won, {len(today) - wins} lost)\n"
                    f"All time realized: {Decimal(self.stats['realized']):+.4f} USDT\n"
                    f"Open trades unrealized: {self._unrealized(self.positions.items()):+.4f} USDT\n"
                    f"Holdings unrealized: {self._unrealized(self.holdings.items()):+.4f} USDT\n"
                    f"(before sell fees)")
        if view == "history":
            if not self.history:
                return "🧾 No closed trades yet."
            lines = ["🧾 <b>Last trades</b>"]
            for h in self.history[-15:][::-1]:
                e = "📦" if h.get("kind") == "hold" else "✅" if Decimal(h["pnl"]) >= 0 else "🔴"
                lines.append(f"{e} {time.strftime('%d %b %H:%M', time.localtime(h['t']))} {h['sym']} "
                             f"{Decimal(h['pnl']):+.3f} ({float(h['pct']):+.2f}%) · {esc(h['reason'])}")
            return "\n".join(lines)
        if view == "holdings":
            if not self.holdings:
                return "📦 No long-term holdings."
            lines = ["📦 <b>Long-term holdings</b>"]
            for sym, h in sorted(self.holdings.items(), key=lambda x: x[1].get("held_at", 0)):
                u = self._pct_now(sym, h["buy_price"])
                lines.append(f"{sym}: {h['qty']} @ {h['buy_price']} (cost {h['cost']}) · now "
                             f"{f'{u:+.2f}%' if u is not None else 'n/a'}")
            return "\n".join(lines[:60])
        if view == "cooldown":
            now = time.time()
            cd = sorted(((s, SYMBOL_COOLDOWN_SEC - (now - t)) for s, t in self.last_buy.items()
                         if now - t < SYMBOL_COOLDOWN_SEC), key=lambda x: x[1])
            if not cd:
                return "⏳ No coins in cooldown."
            return "⏳ <b>Cooldown</b>\n" + "\n".join(
                f"{s}: {int(left) // 3600}h {int(left) % 3600 // 60}m left" for s, left in cd[:60])
        return "?"

    def trade_text(self, sym, p):
        u = self._pct_now(sym, p["buy_price"])
        held = int(time.time()) - p["opened_at"]
        lines = [f"💼 <b>{sym}</b> · {esc(p.get('trigger', ''))}",
                 f"Buy {p['buy_price']} · qty {p['qty']} · cost {Decimal(p['cost']):.2f} USDT",
                 f"Now {self.last_price.get(sym, 'n/a')} ({f'{u:+.2f}%' if u is not None else 'n/a'}) · "
                 f"held {held // 3600}h {held % 3600 // 60}m"]
        if p.get("mode") == "ladder":
            n = len(self.p_targets(p))
            stop = f"{p['stop_label']} @ {p['stop_price']}" if p.get("stop_price") else "none yet"
            lines.append(f"Targets reached {p.get('targets_hit', 0)}/{n} · stop {esc(stop)}"
                         f"{' (on Binance)' if p.get('order_id') else ''}")
            lines.append(f"Next target {p['tp_price']}")
        else:
            lines.append(f"TP sell @ {p['tp_price']}")
        return "\n".join(lines)

    # ── telegram: commands and buttons ──
    async def handle_command(self, cmd, arg="", tb=None):
        reply = tb.send if tb else (lambda t, m=None: self.tg.send(t, markup=m))
        ctl = tb.can_control if tb else True
        menu = kb(MENU if ctl else VIEW_ONLY_MENU)
        alias = {"/open": "/trades", "/p&l": "/pnl", "/profit": "/pnl", "/bal": "/balance"}
        cmd = alias.get(cmd, cmd)
        if cmd in ("/start", "/help"):
            reply(HELP if ctl else VIEW_ONLY_HELP, menu)
        elif cmd == "/menu":
            reply("<b>Pump bot</b> - " + ("control panel" if ctl else "view-only menu"), menu)
        elif cmd in ("/status", "/balance", "/pnl", "/history", "/holdings", "/cooldown"):
            reply(await self.text_for(cmd[1:]), menu if cmd == "/status" else None)
        elif cmd == "/trades":
            self._send_trades(reply)
        elif cmd in ("/kill", "/resume", "/pause", "/closeall"):
            if cmd == "/closeall":
                reply("💥 <b>Market-sell every open trade?</b>\n(Long-term holdings are not touched.)",
                      kb([[("Yes, sell everything", "a:closeall!"), ("Cancel", "v:status")]]))
            else:
                reply(esc(await self.do(cmd[1:])))
        elif cmd == "/sell":
            sym = self._find(arg) if arg else None
            if not sym:
                reply(f"Which trade? e.g. /sell PEPE  (open: {', '.join(self.positions) or 'none'})")
                return
            reply(esc(await self.do("sell", sym)))
        else:
            reply(f"Unknown command {esc(cmd)}. /help", menu)

    async def dispatch(self, data, tb):
        if data.startswith("v:"):
            view = data[2:]
            if view == "trades":
                self._send_trades(tb.send)
                return "Open trades sent"
            text = await self.text_for(view)
            tb.send(text, kb(MENU if tb.can_control else VIEW_ONLY_MENU) if view == "status" else None)
            return text.split("\n")[0].replace("<b>", "").replace("</b>", "")
        if data == "a:closeall":
            tb.send("💥 <b>Market-sell every open trade?</b>\n(Long-term holdings are not touched.)",
                    kb([[("Yes, sell everything", "a:closeall!"), ("Cancel", "v:status")]]))
            return "Confirm below"
        if data.startswith("a:"):
            r = await self.do(data[2:].rstrip("!") if data != "a:closeall!" else "closeall")
            tb.send(esc(r))
            return r
        if data.startswith("p:sell:"):
            r = await self.do("sell", data.split(":", 2)[2])
            tb.send(esc(r))
            return r
        return "?"

    def _send_trades(self, reply):
        if not self.positions:
            reply("💼 No open trades.", None)
            return
        for sym, p in sorted(self.positions.items(), key=lambda x: x[1]["opened_at"]):
            reply(self.trade_text(sym, p), position_buttons(sym))

    def _find(self, s):
        s = s.upper()
        for sym in self.positions:
            if s in (sym, sym[:-len(QUOTE)]) or sym == s + QUOTE:
                return sym
        return None

    async def do(self, action, sym=None):
        if action == "pause":
            self.paused = True
            return "⏸ New entries paused. Open trades and their stops keep running."
        if action == "resume":
            self.paused = False
            self.killed = False
            self.save_state()
            return "▶️ Resumed: KILL and pause cleared, scanning for entries."
        if action == "kill":
            self.killed = True
            self.save_state()
            return ("🛑 KILLED: no new buys (survives restarts). Open trades keep their ladder / stops "
                    "and Binance stop orders. /resume to undo.")
        if action == "closeall":
            if not self.positions:
                return "No open trades."
            res = [await self.sell_now(s, "Close All pressed") for s in list(self.positions)]
            return "💥 Close all:\n" + "\n".join(res)
        if action == "sell":
            return await self.sell_now(sym, "sold by you")
        return "?"

    async def sell_now(self, sym, reason):
        p = self.positions.get(sym)
        if not p:
            return f"{sym}: not an open trade (already closed?)"
        if sym in self.exiting:
            return f"{sym}: busy with another order, try again in a few seconds"
        self.exiting.add(sym)
        resync = False
        try:
            if await self._cancel_order(sym, p) == "filled":
                return f"{sym}: its sell order had just filled on Binance"
            if await self.market_exit(sym, p, "💰", f"SOLD ({reason})"):
                return f"{sym}: sold"
            resync = True
            return f"{sym}: SELL FAILED - value is below Binance's minimum order"
        except Exception as e:
            log.exception("sell_now failed")
            resync = True
            return f"{sym}: SELL FAILED - {e}"
        finally:
            if resync and sym in self.positions:
                asyncio.create_task(self._resync(sym))     # puts the protective stop back, then clears busy
            else:
                self.exiting.discard(sym)

    # ── chart snapshots ──
    def snapshot(self, sym, p, side, caption, exit_price=None):
        """Chart picture + caption on every buy and sell (falls back to text)."""
        markup = position_buttons(sym) if side == "BUY" else None
        if not (TG_SNAPSHOTS and self.tg.enabled):
            self.tg.send(caption, markup=markup)
            return
        asyncio.create_task(self._send_chart(sym, dict(p), side, caption, exit_price, markup))

    async def _send_chart(self, sym, p, side, caption, exit_price, markup):
        try:
            import matplotlib
            matplotlib.use("Agg")
        except Exception:
            self.tg.send(caption, markup=markup)
            return
        try:
            rows = await self.api.klines(sym, "1m", 60)
            buy = float(p["buy_price"])
            lines = {"Entry": buy}
            if p.get("stop_price"):
                lines["Stop"] = float(p["stop_price"])
            if p.get("mode") == "ladder":
                hit = p.get("targets_hit", 0)
                T = self.p_targets(p)
                for k in range(hit + 1, min(len(T), hit + 3) + 1):
                    lines[f"T{k} +{fmt(T[k - 1])}%"] = buy * (1 + float(T[k - 1]) / 100)
            else:
                lines["TP"] = float(p["tp_price"])
            if exit_price:
                lines["Exit"] = float(exit_price)
            markers = [(p["opened_at"] * 1000, buy, "BUY", "#4da3ff")]
            if side == "SELL" and exit_price:
                markers.append((time.time() * 1000, float(exit_price), "SELL", "#ffffff"))
            png = await asyncio.to_thread(render_chart, sym, "1m", rows, lines,
                                          f"{side} {time.strftime('%d %b %H:%M')}", markers)
            self.tg.photo(png, caption, markup)
        except Exception as e:
            log.warning("chart snapshot failed: %r", e)
            self.tg.send(caption, markup=markup)

    async def summary_loop(self):
        if TG_SUMMARY_MIN <= 0:
            return
        last = time.time()
        while True:
            await asyncio.sleep(TG_SUMMARY_MIN * 60)
            try:
                new = [h for h in self.history if h["t"] >= last]
                last = time.time()
                pnl = sum((Decimal(h["pnl"]) for h in new), Decimal(0))
                self.tg.send(
                    f"🕐 <b>Summary (last {TG_SUMMARY_MIN} min)</b>"
                    f"{' · 🛑 KILLED' if self.killed else ' · ⏸ PAUSED' if self.paused else ''}\n"
                    f"Sells {len(new)} · P/L {pnl:+.4f} USDT\n"
                    f"Open {len(self.positions)}/{MAX_OPEN_TRADES} (unrealized "
                    f"{self._unrealized(self.positions.items()):+.4f}) · Holdings {len(self.holdings)}\n"
                    f"All time realized {Decimal(self.stats['realized']):+.4f} USDT")
            except Exception as e:
                log.warning("summary failed: %r", e)

    # ── dashboard ──
    def dash_state(self):
        now = time.time()
        def lp(sym):
            return self.last_price.get(sym)
        pos = []
        for sym, p in sorted(self.positions.items(), key=lambda x: x[1]["opened_at"]):
            buy, qty, cost = float(p["buy_price"]), float(p["qty"]), float(p["cost"])
            cur = lp(sym)
            pos.append({
                "sym": sym, "buy": buy, "qty": qty, "cost": cost, "now": cur,
                "pct": (cur / buy - 1) * 100 if cur and buy else None,
                "pnl": cur * qty - cost if cur else None, "held": now - p["opened_at"],
                "trigger": p.get("trigger", ""), "mode": p.get("mode", "tp"),
                "hit": p.get("targets_hit", 0), "targets": [float(t) for t in self.p_targets(p)]
                if p.get("mode") == "ladder" else [float(TAKE_PROFIT_PCT)],
                "stop": float(p["stop_price"]) if p.get("stop_price") else None,
                "stop_label": p.get("stop_label"), "next": float(p["tp_price"]) if p.get("tp_price") else None,
                "on_binance": bool(p.get("order_id")) and p.get("order_id") != "DRY",
                "busy": sym in self.exiting})
        hold = []
        for sym, h in self.holdings.items():
            buy, qty, cost = float(h["buy_price"]), float(h["qty"]), float(h["cost"])
            cur = lp(sym)
            hold.append({"sym": sym, "buy": buy, "qty": qty, "cost": cost, "now": cur,
                         "pct": (cur / buy - 1) * 100 if cur and buy else None,
                         "pnl": cur * qty - cost if cur else None, "held_at": h.get("held_at")})
        t0 = self._today_start()
        today = [h for h in self.history if h["t"] >= t0 and h.get("kind") != "hold"]
        sells = [h for h in self.history if h.get("kind") != "hold"]
        exposure = sum(x["cost"] for x in pos)
        return {
            "now": now, "mode": "dry" if DRY_RUN else "live",
            "status": "killed" if self.killed else "paused" if self.paused else "running",
            "uptime": now - self.started, "pairs": len(self.streaming),
            "telegram": {"enabled": self.tg.enabled,
                         "bots": [{"name": b.name, "user": b.username, "control": b.can_control} for b in self.tg.bots]},
            "free_usdt": self.free_usdt,
            "kpi": {"exposure": exposure, "cap": float(TRADE_USDT) * MAX_OPEN_TRADES,
                    "open": len(pos), "max_open": MAX_OPEN_TRADES,
                    "today": sum(float(h["pnl"]) for h in today), "today_n": len(today),
                    "realized": float(self.stats["realized"]),
                    "unrealized": sum(x["pnl"] or 0 for x in pos),
                    "hold_unrealized": sum(x["pnl"] or 0 for x in hold),
                    "wins": sum(1 for h in sells if float(h["pnl"]) >= 0),
                    "losses": sum(1 for h in sells if float(h["pnl"]) < 0),
                    "buys": self.stats["buys"]},
            "positions": pos, "holdings": hold,
            "cooldown": sorted(({"sym": k, "left": SYMBOL_COOLDOWN_SEC - (now - v)} for k, v in self.last_buy.items()
                                if now - v < SYMBOL_COOLDOWN_SEC), key=lambda x: x["left"]),
            "signals": list(self.signals)[::-1],
            "history": self.history[::-1],
            "events": list(EVENTS)[::-1][:300],
            "settings": {
                "Mode": "DRY RUN" if DRY_RUN else "LIVE", "Trade size (USDT)": fmt(TRADE_USDT),
                "Buy signal": f"1m ≥ {PUMP_1M_PCT}% or 1s ≥ {PUMP_1S_PCT}%",
                "Min volume in candle (1s / 1m)": f"{MIN_QUOTE_VOL_1S} / {MIN_QUOTE_VOL_1M} USDT",
                "Max open trades": MAX_OPEN_TRADES, "Cooldown per coin": f"{SYMBOL_COOLDOWN_SEC / 3600:g} h",
                "Exit mode": EXIT_MODE,
                "Ladder targets": " / ".join("+" + fmt(t) + "%" for t in LADDER_TARGETS),
                "Stop after a target": (f"one target behind (T1 → +{fmt(LADDER_FIRST_STOP_PCT)}%)"
                                        if LADDER_STOP_MODE == "previous" else "at the target hit"),
                "Sell at last target": LADDER_SELL_AT_LAST, "Stop order on Binance": LADDER_EXCHANGE_STOP,
                "Stop-loss before T1": f"-{fmt(STOP_LOSS_PCT)}%" if STOP_LOSS_ENABLED else "off",
                "Long-term hold at": f"-{fmt(HOLD_DRAWDOWN_PCT)}%" if HOLD_DRAWDOWN_PCT > 0 else "off",
                "Skip coins in holdings": SKIP_HELD_COINS,
                "Skip 24h losers": f"yes, below {MIN_24H_CHANGE_PCT:g}%" if SKIP_24H_LOSERS else "no",
                "TP (tp mode only)": f"+{fmt(TAKE_PROFIT_PCT)}%",
                "Excluded suffix": EXCLUDE_SUFFIX, "Keep symbols": ", ".join(sorted(KEEP_SYMBOLS)) or "-",
                "Extra excluded": ", ".join(sorted(EXTRA_EXCLUDE)) or "-",
                "Telegram snapshots": TG_SNAPSHOTS, "Telegram summary": f"{TG_SUMMARY_MIN} min",
            },
        }

    async def dash_chart(self, sym):
        sym = sym.upper()
        if sym not in self.meta:
            raise ValueError(f"unknown symbol {sym}")
        rows = await self.api.klines(sym, "1m", 120)
        p = self.positions.get(sym) or self.holdings.get(sym)
        lines = {}
        if p:
            buy = float(p["buy_price"])
            lines["Entry"] = buy
            if p.get("stop_price") and sym in self.positions:
                lines["Stop"] = float(p["stop_price"])
            if p.get("mode") == "ladder" and sym in self.positions:
                hit = p.get("targets_hit", 0)
                T = self.p_targets(p)
                for k in range(hit + 1, min(len(T), hit + 3) + 1):
                    lines[f"T{k} +{fmt(T[k - 1])}%"] = buy * (1 + float(T[k - 1]) / 100)
            elif sym in self.positions:
                lines["TP"] = float(p["tp_price"])
        return {"symbol": sym, "candles": [[int(r[0]) // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4])]
                                           for r in rows], "lines": lines,
                "opened_at": p.get("opened_at") if p else None}

    async def balance_loop(self):
        while True:
            if not DRY_RUN:
                try:
                    self.free_usdt = float(await self.api.balance(QUOTE))
                except Exception as e:
                    log.debug("balance refresh failed: %s", e)
            await asyncio.sleep(30)

    async def start_dashboard(self):
        if not DASHBOARD_ENABLED:
            return
        from aiohttp import web
        page = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")

        def check(req):
            if DASHBOARD_TOKEN and not _hmac.compare_digest(req.headers.get("x-token", ""), DASHBOARD_TOKEN):
                raise web.HTTPUnauthorized(text="bad token")

        async def index(req):
            return web.FileResponse(page, headers={"Cache-Control": "no-store"})

        async def state(req):
            check(req)
            return web.json_response(self.dash_state(), dumps=lambda o: json.dumps(o, default=str))

        async def chart(req):
            check(req)
            try:
                return web.json_response(await self.dash_chart(req.query.get("symbol", "")))
            except ValueError as e:
                raise web.HTTPBadRequest(text=str(e))
            except Exception as e:
                raise web.HTTPBadGateway(text=f"chart data unavailable: {e}")

        async def action(req):
            check(req)
            if req.headers.get("x-bot") != "1":      # blocks cross-site form posts
                raise web.HTTPForbidden(text="missing header")
            body = await req.json()
            act, sym = str(body.get("action", "")), body.get("id")
            if act not in ("kill", "resume", "pause", "closeall", "sell"):
                raise web.HTTPBadRequest(text="unknown action")
            log.info("dashboard action: %s %s", act, sym or "")
            msg = await self.do(act, sym)
            if act in ("kill", "resume", "pause"):
                self.tg.send(f"{msg} (from the dashboard)")
            return web.json_response({"ok": True, "message": msg})

        app = web.Application()
        app.router.add_get("/", index)
        app.router.add_get("/api/state", state)
        app.router.add_get("/api/chart", chart)
        app.router.add_post("/api/action", action)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, DASHBOARD_HOST, DASHBOARD_PORT).start()
        except OSError as e:
            log.error("Dashboard NOT started: port %s is in use (%s). Another copy of this bot running? "
                      "Or change DASHBOARD_PORT.", DASHBOARD_PORT, e)
            self.tg.send(f"⚠️ Dashboard not started: port {DASHBOARD_PORT} is already in use.")
            return
        self.dash_url = f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}"
        log.info("Dashboard: %s", self.dash_url)

    # ── run ──
    async def run(self):
        if not DRY_RUN and not (API_KEY and API_SECRET):
            raise SystemExit("Set BINANCE_API_KEY / BINANCE_API_SECRET or use DRY_RUN=true")
        asyncio.create_task(self.tg.sender())
        self.dash_url = ""
        await self.start_dashboard()
        await self.tg.setup()
        await self.api.sync_time()
        syms, excluded = await self.load_symbols()
        try:
            await self.refresh_24h()
        except Exception as e:
            log.warning("24h tickers not loaded yet: %s", e)
        self.start_streams(syms)

        bal = ""
        if not DRY_RUN:
            b = await self.api.balance(QUOTE)
            bal = f"\nFree USDT: {fmt(b.quantize(Decimal('0.01')))}"
        bstocks = [s for s in excluded if s.endswith(EXCLUDE_SUFFIX)]
        self.tg.send(keyboard=True, text=
            f"🚀 <b>Pump bot started</b> {'[DRY RUN]' if DRY_RUN else '[LIVE]'}"
            f"{(' · @' + self.tg.username) if self.tg.username else ''}"
            f"{(' · +' + str(len(self.tg.bots) - 1) + ' extra chat(s)') if len(self.tg.bots) > 1 else ''}\n"
            f"Scanning {len(syms)} USDT pairs (1m ≥ {PUMP_1M_PCT}% or 1s ≥ {PUMP_1S_PCT}%)\n"
            f"Trade size {fmt(TRADE_USDT)} USDT · "
            + (f"🪜 Ladder {' / '.join('+' + fmt(t) + '%' for t in LADDER_TARGETS)}"
               f"{' (last sells)' if LADDER_SELL_AT_LAST else ''}"
               f"{' · stop on Binance' if LADDER_EXCHANGE_STOP else ''}"
               f"{(' · stop trails 1 target (T1 → +' + fmt(LADDER_FIRST_STOP_PCT) + '%)') if LADDER_STOP_MODE == 'previous' else ''}"
               if EXIT_MODE == "ladder" else f"TP +{fmt(TAKE_PROFIT_PCT)}%") + " · "
            f"{('stop-loss -' + fmt(STOP_LOSS_PCT) + '%') if STOP_LOSS_ENABLED else 'no stop-loss before T1' if EXIT_MODE == 'ladder' else 'no stop-loss'}\n"
            f"Hold at -{fmt(HOLD_DRAWDOWN_PCT)}% (TP cancelled, kept long-term) · "
            f"Cooldown {SYMBOL_COOLDOWN_SEC // 3600}h per coin"
            f"{(' · skip 24h losers (below ' + format(MIN_24H_CHANGE_PCT, 'g') + '%)') if SKIP_24H_LOSERS else ''}\n"
            f"Max open {MAX_OPEN_TRADES} · Resumed positions {len(self.positions)} · "
            f"Holdings {len(self.holdings)}{bal}\n"
            f"{('🖥 Dashboard: ' + self.dash_url + chr(10)) if self.dash_url else ''}"
            f"Excluded bStocks ({len(bstocks)}): {', '.join(bstocks[:40])}"
            f"{' …' if len(bstocks) > 40 else ''}")

        if self.startup_notes:
            self.tg.send("🧹 <b>State cleaned up</b>\n" + "\n".join("• " + esc(n) for n in self.startup_notes))
        if self.killed:
            self.tg.send("🛑 KILL is still on from before the restart: no new buys. /resume to clear it.")
        await asyncio.gather(self.monitor(), self.refresh_symbols(), self.tg.poll_commands(self),
                             self.summary_loop(), self.balance_loop(), self.ticker_loop())


async def main():
    async with aiohttp.ClientSession() as s:
        bot = Bot(s)
        try:
            await bot.run()
        finally:
            bot.save_state()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped")
