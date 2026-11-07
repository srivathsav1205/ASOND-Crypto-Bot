#!/usr/bin/env python3
"""Rule-based Binance Spot swing bot. Live trading is opt-in via ENABLE_LIVE_TRADING=true."""
from __future__ import annotations
import json, logging, os, signal, threading, time, urllib.parse, urllib.request, urllib.error, hmac, hashlib, random, math, secrets
DASHBOARD_TOKEN = secrets.token_hex(16)
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
# This LIVE candidate stores its state separately from the Testnet project.
# Production endpoint is explicit and fail-closed; Testnet URLs are refused.
BASE_URL = os.getenv("BINANCE_BASE_URL", "").rstrip("/")
if not BASE_URL:
    raise RuntimeError(
        "BINANCE_BASE_URL must be explicitly set in .env; refusing to guess an endpoint."
    )
ALLOWED_BASE_URLS = {
    "https://api.binance.com",
    "https://data-api.binance.vision",
    "https://testnet.binance.vision",
}
if BASE_URL not in ALLOWED_BASE_URLS:
    raise RuntimeError(
        f"BINANCE_BASE_URL must be one of {ALLOWED_BASE_URLS}; refusing unknown endpoint."
    )
API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
LIVE = os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true"
if LIVE and BASE_URL == "https://data-api.binance.vision":
    raise RuntimeError(
        "BINANCE_BASE_URL=https://data-api.binance.vision is market-data only and does not support signed trading orders. "
        "Set BINANCE_BASE_URL=https://api.binance.com when ENABLE_LIVE_TRADING=true."
    )

is_testnet_env = (BASE_URL == "https://testnet.binance.vision")
if is_testnet_env:
    default_data_file = ROOT / "bot_state_testnet.json"
elif LIVE:
    default_data_file = ROOT / "bot_state_allcoins_live.json"
else:
    default_data_file = ROOT / "bot_state_signal_only.json"
DATA_FILE = Path(os.getenv("BOT_STATE_FILE", str(default_data_file)))

def _validate_state_file_mode(path: Path, is_live: bool, base_url: str = ""):
    name = path.name.lower()
    if base_url == "https://testnet.binance.vision":
        if "testnet" not in name:
            raise RuntimeError(
                f"FATAL: Pointed to Binance Testnet ({base_url}), but state file '{path.name}' is not a testnet state file. "
                f"Expected '*testnet*.json'. Refusing to write testnet data into live or paper state."
            )
        if "allcoins_live" in name:
            raise RuntimeError(
                f"FATAL: Testnet must never write to production live state file '{path.name}'."
            )
    elif is_live:
        if "testnet" in name:
            raise RuntimeError(
                f"FATAL: In production live mode ({base_url}), state file '{path.name}' cannot be a testnet state file. "
                f"Refusing testnet state in production live mode."
            )
        if "paper" in name or "signal_only" in name or "live" not in name:
            raise RuntimeError(
                f"FATAL: In live mode (ENABLE_LIVE_TRADING=true), state file '{path.name}' must match '*live*.json' "
                f"and cannot contain 'paper', 'signal_only', or 'testnet'."
            )
    else:
        if "testnet" in name:
            raise RuntimeError(
                f"FATAL: In paper mode (ENABLE_LIVE_TRADING=false), state file '{path.name}' cannot be a testnet state file. "
                f"Expected '*signal_only*.json' or '*paper*.json'."
            )
        if "live" in name and ("paper" not in name and "signal_only" not in name):
            raise RuntimeError(
                f"FATAL: In paper mode (ENABLE_LIVE_TRADING=false), state file '{path.name}' cannot be a live trading state file (*live*.json). "
                f"Expected '*signal_only*.json' or '*paper*.json'."
            )

_validate_state_file_mode(DATA_FILE, LIVE, BASE_URL)
if is_testnet_env:
    default_log_file = ROOT / "bot_testnet.log"
elif LIVE:
    default_log_file = ROOT / "bot_allcoins_live.log"
else:
    default_log_file = ROOT / "bot_signal_only.log"
LOG_FILE = Path(os.getenv("BOT_LOG_FILE", str(default_log_file)))
# The same closed-candle trend/breakout/retest strategy runs for every eligible pair.
# Scan all active USDT-quoted Spot pairs that meet the liquidity threshold, in batches.
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
# Wallet holdings are independent of the trading universe. Exclude BNBUSDT by default
# so BNB can remain in Spot for fee deduction without being traded by this strategy.
EXCLUDED_SYMBOLS = {s.strip().upper() for s in os.getenv("EXCLUDED_SYMBOLS", "BNBUSDT").split(",") if s.strip()}
BLACKLISTED_SYMBOLS = {s.strip().upper() for s in os.getenv("BLACKLISTED_SYMBOLS", "").split(",") if s.strip()}
SCAN_ALL_USDT_SPOT = os.getenv("SCAN_ALL_USDT_SPOT", "true").lower() == "true"
QUOTE_ASSET = os.getenv("QUOTE_ASSET", "USDT").strip().upper()
MIN_24H_QUOTE_VOLUME_USDT = float(os.getenv("MIN_24H_QUOTE_VOLUME_USDT", "1000000"))
SCAN_BATCH_SIZE = int(os.getenv("SCAN_BATCH_SIZE", "15"))
TICKER_REFRESH_SECONDS = int(os.getenv("TICKER_REFRESH_SECONDS", "900"))
UNIVERSE_REFRESH_SECONDS = int(os.getenv("UNIVERSE_REFRESH_SECONDS", "21600"))
EQUITY_CHANGE_REVIEW_THRESHOLD_USDT = float(os.getenv("EQUITY_CHANGE_REVIEW_THRESHOLD_USDT", "1.0"))
RISK_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "2")) / 100
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "10")) / 100  # Legacy dashboard/config compatibility; fixed trade notional controls sizing.
TRADE_NOTIONAL_USDT = Decimal(os.getenv("TRADE_NOTIONAL_USDT", "20"))
ENTRY_ORDER_TIMEOUT_SECONDS = int(os.getenv("ENTRY_ORDER_TIMEOUT_SECONDS", "300"))

# Exit Mode & Stop Distance Guard Configuration
EXIT_MODE = os.getenv("EXIT_MODE", "FIXED_TP").strip().upper()  # "2R" or "FIXED_TP"
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "1.5")) / 100
MIN_STOP_PCT = float(os.getenv("MIN_STOP_PCT", "1.5")) / 100
MAX_STOP_PCT = float(os.getenv("MAX_STOP_PCT", "4.0")) / 100

MAX_EXPOSURE_PCT = float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "60")) / 100
RESERVE_PCT = float(os.getenv("MIN_CASH_RESERVE_PCT", "40")) / 100
MAX_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
DAILY_LOSS_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "2")) / 100
WEEKLY_LOSS_PCT = float(os.getenv("WEEKLY_LOSS_LIMIT_PCT", "4")) / 100
DRAWDOWN_PCT = float(os.getenv("MAX_DRAWDOWN_PCT", "8")) / 100
MAX_SLIPPAGE = float(os.getenv("MAX_ENTRY_SLIPPAGE_PCT", "0.10")) / 100
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8767"))
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Market-Context Gates (FINAL_PLAN.md Steps 3 & 4)
ENABLE_BTC_REGIME_GATE = os.getenv("ENABLE_BTC_REGIME_GATE", "true").lower() == "true"
ENABLE_RS_FILTER = os.getenv("ENABLE_RS_FILTER", "true").lower() == "true"
ENABLE_15M_FILTER = os.getenv("ENABLE_15M_FILTER", "false").lower() == "true"
MIN_SCORECARD_POINTS = int(os.getenv("MIN_SCORECARD_POINTS", "0"))
MAX_HOLD_HOURS = int(os.getenv("MAX_HOLD_HOURS", "0"))  # 0 disables time stop (holds strictly until TP or SL)
# Paper-trading realism (defaults match backtest.py): percent units.
PAPER_SLIPPAGE = float(os.getenv("PAPER_SLIPPAGE_PCT", "0.05")) / 100
STOP_SLIPPAGE = float(os.getenv("STOP_SLIPPAGE_PCT", "0.20")) / 100
PAPER_FEE = float(os.getenv("PAPER_FEE_PCT", "0.1")) / 100

TERMINAL_TRADE_STATUSES = (
    "CLOSED",
    "CLOSED_TARGET",
    "CLOSED_STOP",
    "CLOSED_TIME_STOP",
    "CLOSED_MANUAL_MARKET",
    "CLOSED_STOP_FALLBACK",
    "CLOSED_OCO_STOP",
    "CLOSED_OCO_TARGET",
    "CLOSED_OCO_EXIT",
)

STABLE_OR_FIAT_BASE_ASSETS = {
    "USDC", "FDUSD", "BUSD", "TUSD", "USDP", "DAI", "USDE", "USDS",
    "USDD", "USD1", "USTC", "EUR", "EURC", "AEUR", "TRY", "BRL", "BIDR",
    "IDRT", "NGN", "RUB", "UAH", "ZAR", "PLN", "RON", "ARS", "JPY",
}
LEVERAGED_TOKEN_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR", "2L", "2S", "3L", "3S", "5L", "5S")

if QUOTE_ASSET != "USDT":
    raise RuntimeError("This project is intentionally limited to USDT-quoted Spot pairs.")
if not SCAN_ALL_USDT_SPOT and not SYMBOLS:
    raise RuntimeError("Set SYMBOLS when SCAN_ALL_USDT_SPOT=false.")
if MIN_24H_QUOTE_VOLUME_USDT < 0:
    raise RuntimeError("MIN_24H_QUOTE_VOLUME_USDT must be >=0.")
if not (1 <= SCAN_BATCH_SIZE <= 50):
    raise RuntimeError("SCAN_BATCH_SIZE must be between 1 and 50 to control API usage.")
if TICKER_REFRESH_SECONDS < 300:
    raise RuntimeError("TICKER_REFRESH_SECONDS must be at least 300 seconds.")
if UNIVERSE_REFRESH_SECONDS < 1800:
    raise RuntimeError("UNIVERSE_REFRESH_SECONDS must be at least 1800 seconds.")
if EQUITY_CHANGE_REVIEW_THRESHOLD_USDT < 0.01:
    raise RuntimeError("EQUITY_CHANGE_REVIEW_THRESHOLD_USDT must be >=0.01.")
if not (0 < RISK_PCT <= 0.02):
    raise RuntimeError("RISK_PER_TRADE_PCT must be >0 and <=2.0.")
if not (0 < MAX_POSITION_PCT <= 0.10):
    raise RuntimeError("MAX_POSITION_PCT must be >0 and <=10.")
if not (0 < MAX_EXPOSURE_PCT <= 0.60):
    raise RuntimeError("MAX_TOTAL_EXPOSURE_PCT must be >0 and <=60.")
if not (0.40 <= RESERVE_PCT < 1.0):
    raise RuntimeError("MIN_CASH_RESERVE_PCT must be >=40 and <100.")
if not (1 <= MAX_POSITIONS <= 5):
    raise RuntimeError("MAX_OPEN_POSITIONS must be between 1 and 5.")
if TRADE_NOTIONAL_USDT <= 0:
    raise RuntimeError("TRADE_NOTIONAL_USDT must be >0.")
if ENTRY_ORDER_TIMEOUT_SECONDS < 30 or ENTRY_ORDER_TIMEOUT_SECONDS > 900:
    raise RuntimeError("ENTRY_ORDER_TIMEOUT_SECONDS must be between 30 and 900.")
if EXIT_MODE not in {"2R", "FIXED_TP"}:
    raise RuntimeError("EXIT_MODE must be either '2R' or 'FIXED_TP'.")
if not (0 < TAKE_PROFIT_PCT <= 0.10):
    raise RuntimeError("TAKE_PROFIT_PCT must be >0 and <=10.")
if not (0.005 <= MIN_STOP_PCT < MAX_STOP_PCT <= 0.10):
    raise RuntimeError("MIN_STOP_PCT and MAX_STOP_PCT must satisfy 0.5% <= MIN_STOP_PCT < MAX_STOP_PCT <= 10.0%.")
if MAX_HOLD_HOURS < 0:
    raise RuntimeError("MAX_HOLD_HOURS must be >= 0 (0 disables time stop).")
if not (0 < DAILY_LOSS_PCT <= 0.02):
    raise RuntimeError("DAILY_LOSS_LIMIT_PCT must be >0 and <=2.0.")
if not (0 < WEEKLY_LOSS_PCT <= 0.04):
    raise RuntimeError("WEEKLY_LOSS_LIMIT_PCT must be >0 and <=4.0.")
if not (0 < DRAWDOWN_PCT <= 0.08):
    raise RuntimeError("MAX_DRAWDOWN_PCT must be >0 and <=8.0.")
if not (0 <= MAX_SLIPPAGE <= 0.001):
    raise RuntimeError("MAX_ENTRY_SLIPPAGE_PCT must be between 0 and 0.10.")
if POLL_SECONDS < 5:
    raise RuntimeError("POLL_SECONDS must be at least 5 seconds.")
if DASHBOARD_HOST not in {"127.0.0.1", "localhost"}:
    raise RuntimeError("DASHBOARD_HOST must be localhost; remote dashboard binding is disabled.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()])
log = logging.getLogger("swingbot")
stop_event = threading.Event()

class BinanceError(RuntimeError): pass

def compute_target_price(entry_price: float, stop_price: float) -> float:
    """Compute target price based on EXIT_MODE ('FIXED_TP' vs '2R')."""
    if EXIT_MODE == "FIXED_TP":
        return entry_price * (1.0 + TAKE_PROFIT_PCT)
    return entry_price + 2.0 * (entry_price - stop_price)

def is_eligible_usdt_spot_item(item: dict[str, Any]) -> bool:
    """Return True for active, liquid-universe candidates: regular USDT Spot pairs only."""
    if item.get("status") != "TRADING" or item.get("isSpotTradingAllowed") is not True:
        return False
    if item.get("quoteAsset") != "USDT":
        return False
    base = str(item.get("baseAsset", "")).upper()
    symbol = str(item.get("symbol", "")).upper()
    if not base or not symbol.endswith("USDT"):
        return False
    if base in STABLE_OR_FIAT_BASE_ASSETS:
        return False
    if base.endswith(LEVERAGED_TOKEN_SUFFIXES):
        return False
    return True


def rotating_batch(symbols: list[str], start_index: int, batch_size: int) -> tuple[list[str], int]:
    """Return a deterministic round-robin batch and the next cursor."""
    if not symbols or batch_size <= 0:
        return [], 0
    count = min(batch_size, len(symbols))
    start = start_index % len(symbols)
    batch = [symbols[(start + i) % len(symbols)] for i in range(count)]
    return batch, (start + count) % len(symbols)


def flow_adjusted_equity(account_equity_usdt: float, net_external_flows_usdt: float) -> float:
    """Equity used for risk limits after excluding explicitly recorded net deposits/withdrawals."""
    actual = float(account_equity_usdt)
    flows = float(net_external_flows_usdt)
    if not math.isfinite(actual) or not math.isfinite(flows):
        raise ValueError("Equity and external-flow amounts must be finite numbers.")
    adjusted = actual - flows
    if not math.isfinite(adjusted):
        raise ValueError("Flow-adjusted equity is not finite.")
    return adjusted


class Binance:
    def __init__(self):
        if LIVE and (not API_KEY or not API_SECRET):
            raise RuntimeError("Set BINANCE_API_KEY and BINANCE_API_SECRET in .env for live trading.")
        self.time_offset: int = 0
        if LIVE or is_testnet_env:
            self.sync_time()
        self.filters: dict[str, dict[str, Any]] = {}
        self.all_spot_usdt_symbols: list[str] = []
        self.symbols: list[str] = []
        self.relative_strength: dict[str, float] = {}
        self.last_exchange_info_at = 0.0
        self.last_ticker_refresh_at = 0.0
        self.exchange_info()
        self.refresh_ticker_universe(force=True)

    def sync_time(self):
        """Synchronize client timestamp with Binance server time to prevent -1021 INVALID_TIMESTAMP."""
        try:
            req = urllib.request.Request(f"{BASE_URL}/api/v3/time", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                server_time = int(data.get("serverTime", 0))
                if server_time > 0:
                    self.time_offset = server_time - int(time.time() * 1000)
                    log.info("Synchronized Binance server time: offset=%d ms", self.time_offset)
        except Exception as err:
            log.warning("Could not sync Binance server time: %s; using local time", err)
            self.time_offset = 0

    def request(self, method: str, path: str, params: dict[str, Any] | None = None, signed: bool = False, timeout: int = 20):
        """Make a Binance request. Retry only safe GETs; never blindly replay order writes."""
        method = method.upper()
        base_params = dict(params or {})
        max_attempts = 4 if method == "GET" else 1
        last_error = None

        for attempt in range(max_attempts):
            request_params = dict(base_params)
            headers = {
                "X-MBX-APIKEY": API_KEY,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            }
            if signed:
                request_params["recvWindow"] = 10000
                request_params["timestamp"] = int(time.time() * 1000) + getattr(self, "time_offset", 0) - 500
                query = urllib.parse.urlencode(request_params)
                request_params["signature"] = hmac.new(
                    API_SECRET.encode(), query.encode(), hashlib.sha256
                ).hexdigest()

            data = urllib.parse.urlencode(request_params).encode() if method in ("POST", "PUT", "DELETE") else None
            url = BASE_URL + path
            if method not in ("POST", "PUT", "DELETE") and request_params:
                url += "?" + urllib.parse.urlencode(request_params)
            req = urllib.request.Request(url, data=data, headers=headers, method=method)

            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    payload = response.read().decode()
                    return json.loads(payload) if payload else {}
            except urllib.error.HTTPError as exc:
                body = exc.read().decode(errors="replace")
                # Retry read-only requests on transient server/rate-limit responses only.
                if method == "GET" and exc.code in (429, 500, 502, 503, 504) and attempt < max_attempts - 1:
                    delay = min(2 ** attempt, 8) + random.uniform(0, 0.25)
                    log.warning("Transient Binance HTTP %s for %s; retrying in %.1fs", exc.code, path, delay)
                    time.sleep(delay)
                    continue
                raise BinanceError(f"Binance HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if method == "GET" and attempt < max_attempts - 1:
                    delay = min(2 ** attempt, 8) + random.uniform(0, 0.25)
                    log.warning("Temporary connection error for Binance %s: %s; retrying in %.1fs", path, exc, delay)
                    time.sleep(delay)
                    continue
                if method in ("POST", "PUT"):
                    raise BinanceError(
                        f"Uncertain outcome for Binance {method} {path}; do not repeat blindly. Check exchange orders first. Cause: {exc}"
                    ) from exc
                raise BinanceError(f"Binance request failed: {exc}") from exc
            except Exception as exc:
                raise BinanceError(f"Binance request failed: {exc}") from exc

        raise BinanceError(f"Binance GET failed after retries: {last_error}")

    def exchange_info(self):
        """Load active Spot filters and the complete eligible USDT pair universe."""
        if SCAN_ALL_USDT_SPOT:
            data = self.request("GET", "/api/v3/exchangeInfo", timeout=35)
        else:
            data = self.request(
                "GET", "/api/v3/exchangeInfo",
                {"symbols": json.dumps(SYMBOLS, separators=(",", ":"))},
                timeout=35,
            )
        new_filters: dict[str, dict[str, Any]] = {}
        eligible: list[str] = []
        returned = {item.get("symbol") for item in data.get("symbols", [])}
        for item in data.get("symbols", []):
            symbol = str(item.get("symbol", "")).upper()
            if symbol in EXCLUDED_SYMBOLS or symbol in BLACKLISTED_SYMBOLS:
                continue
            if SCAN_ALL_USDT_SPOT:
                if not is_eligible_usdt_spot_item(item):
                    continue
            else:
                if symbol not in SYMBOLS:
                    continue
                if item.get("status") != "TRADING" or item.get("isSpotTradingAllowed") is not True:
                    raise RuntimeError(f"Configured symbol {symbol} is not enabled for Spot trading")
            filters = {f["filterType"]: f for f in item.get("filters", [])}
            if not all(k in filters for k in ("PRICE_FILTER", "LOT_SIZE")):
                continue
            new_filters[symbol] = filters
            eligible.append(symbol)
        if not SCAN_ALL_USDT_SPOT:
            explicitly_excluded = sorted(set(SYMBOLS) & (EXCLUDED_SYMBOLS | BLACKLISTED_SYMBOLS))
            if explicitly_excluded:
                raise RuntimeError(f"Configured symbols are excluded from trading: {explicitly_excluded}")
            missing = sorted(set(SYMBOLS) - returned)
            if missing:
                raise RuntimeError(f"Configured symbols were not returned by exchangeInfo: {missing}")
            if set(eligible) != set(SYMBOLS):
                raise RuntimeError("One or more configured symbols did not pass Spot eligibility checks")
        if not eligible:
            raise RuntimeError("No eligible USDT Spot symbols were found; refusing to scan/trade.")
        self.filters = new_filters
        self.all_spot_usdt_symbols = sorted(set(eligible))
        if not SCAN_ALL_USDT_SPOT:
            self.symbols = list(SYMBOLS)
        self.last_exchange_info_at = time.time()
        log.info("Exchange symbol metadata loaded: eligible pairs=%d", len(self.all_spot_usdt_symbols))

    def refresh_ticker_universe(self, force: bool = False) -> list[str]:
        """Refresh 24h quote-volume data and rank eligible pairs for round-robin scanning."""
        now = time.time()
        if not SCAN_ALL_USDT_SPOT:
            self.symbols = list(SYMBOLS)
            self.last_ticker_refresh_at = now
            return self.symbols
        if not force and now - self.last_ticker_refresh_at < TICKER_REFRESH_SECONDS:
            return self.symbols
        try:
            tickers = self.request("GET", "/api/v3/ticker/24hr", timeout=35)
        except Exception as exc:
            if self.symbols:
                log.warning(
                    "Periodic 24h ticker refresh failed (%s); retaining existing universe of %d symbols and retrying in 60s.",
                    exc, len(self.symbols),
                )
                self.last_ticker_refresh_at = now - TICKER_REFRESH_SECONDS + 60
                return self.symbols
            raise
        if not isinstance(tickers, list):
            if self.symbols:
                log.warning(
                    "24h ticker response was not a list; retaining existing universe of %d symbols and retrying in 60s.",
                    len(self.symbols),
                )
                self.last_ticker_refresh_at = now - TICKER_REFRESH_SECONDS + 60
                return self.symbols
            raise RuntimeError("24h ticker response was not a list; refusing to trade with unknown liquidity.")
        volumes = {}
        for row in tickers:
            try:
                symbol = str(row["symbol"]).upper()
                quote_volume = float(row.get("quoteVolume", 0))
                if math.isfinite(quote_volume) and quote_volume >= MIN_24H_QUOTE_VOLUME_USDT:
                    volumes[symbol] = quote_volume
            except (KeyError, TypeError, ValueError):
                continue
        btc_change = 0.0
        for row in tickers:
            if str(row.get("symbol", "")).upper() == "BTCUSDT":
                try:
                    btc_change = float(row.get("priceChangePercent", 0.0))
                except (ValueError, TypeError):
                    btc_change = 0.0
                break

        rs_map = {}
        price_map = {}
        for row in tickers:
            sym = str(row.get("symbol", "")).upper()
            try:
                change = float(row.get("priceChangePercent", 0.0))
                rs_map[sym] = round(change - btc_change, 2)
                price_map[sym] = float(row.get("lastPrice", 0.0))
            except (ValueError, TypeError):
                continue
        self.relative_strength = rs_map
        self.latest_prices = price_map

        selected = [s for s in self.all_spot_usdt_symbols if s in volumes]
        selected.sort(key=lambda s: (-volumes[s], s))
        if not selected:
            if self.symbols:
                log.warning("No USDT Spot symbols passed volume filter; retaining existing universe.")
                self.last_ticker_refresh_at = now - TICKER_REFRESH_SECONDS + 60
                return self.symbols
            raise RuntimeError(
                "No USDT Spot symbols meet MIN_24H_QUOTE_VOLUME_USDT; refusing to scan/trade."
            )
        self.symbols = selected
        self.last_ticker_refresh_at = now
        log.info(
            "USDT Spot scan universe refreshed: %d eligible pairs, %d pass 24h liquidity floor (%g USDT).",
            len(self.all_spot_usdt_symbols), len(self.symbols), MIN_24H_QUOTE_VOLUME_USDT,
        )
        state.event(
            f"USDT Spot universe refreshed: {len(self.symbols)} liquid pairs (floor > ${MIN_24H_QUOTE_VOLUME_USDT/1e6:.1f}M USDT)"
        )
        return self.symbols

    def refresh_universe_if_due(self) -> list[str]:
        now = time.time()
        if SCAN_ALL_USDT_SPOT and now - self.last_exchange_info_at >= UNIVERSE_REFRESH_SECONDS:
            try:
                self.exchange_info()
                self.last_ticker_refresh_at = 0.0
            except Exception as exc:
                if self.symbols:
                    log.warning("Periodic exchangeInfo refresh failed (%s); retaining existing universe.", exc)
                    self.last_exchange_info_at = now - UNIVERSE_REFRESH_SECONDS + 300
                else:
                    raise
        return self.refresh_ticker_universe(force=False)

    def klines(self, symbol, interval, limit=220, start_time=None):
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = int(start_time)
        return self.request("GET", "/api/v3/klines", params)

    def price(self, symbol):
        return float(self.request("GET", "/api/v3/ticker/price", {"symbol": symbol})["price"])

    def account(self):
        if not API_KEY or not API_SECRET:
            return {"balances": [{"asset": "USDT", "free": "1000.0", "locked": "0.0"}]}
        return self.request("GET", "/api/v3/account", signed=True)

    def order(self, params):
        return self.request("POST", "/api/v3/order", params, signed=True)

    def query_order(self, symbol, order_id):
        return self.request("GET", "/api/v3/order", {"symbol": symbol, "orderId": order_id}, signed=True)

    def cancel_order(self, symbol, order_id):
        return self.request("DELETE", "/api/v3/order", {"symbol": symbol, "orderId": order_id}, signed=True)

    def oco_sell(self, symbol, qty, target, stop, stop_limit):
        # Binance Spot OCO order-list API parameters (above/below order model).
        return self.request("POST", "/api/v3/orderList/oco", {
            "symbol": symbol, "side": "SELL", "quantity": qty,
            "aboveType": "LIMIT_MAKER", "abovePrice": target,
            "belowType": "STOP_LOSS_LIMIT", "belowStopPrice": stop,
            "belowPrice": stop_limit, "belowTimeInForce": "GTC"
        }, signed=True)

@dataclass
class Position:
    symbol: str
    quantity: str
    entry_price: float
    stop_price: float
    target_price: float
    entry_order_id: int | None = None
    oco_order_list_id: int | None = None
    opened_at: float = 0
    breakout_level: float = 0

class State:
    def __init__(self):
        self.lock = threading.RLock()
        self._last_saved_payload = None
        self.data = {
            "positions": {},
            "setups": {},
            "trades": [],
            "events": [],
            "day_key": "",
            "day_start_equity": None,
            "week_key": "",
            "week_start_equity": None,
            "peak_equity": None,
            "paused_until": 0,
            "manual_pause": False,
            "manual_pause_reason": None,
            "net_external_flows_usdt": 0.0,
            "external_flow_ledger": [],
            "last_account_equity_usdt": None,
            "account_change_requires_review": None,
            "last_hour_close": {},
            "last_15m_close": {},
            "setups_15m": {},
            "confirmed_15m_patterns": {},
        }

        if DATA_FILE.exists():
            try:
                loaded = json.loads(DATA_FILE.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or not isinstance(
                    loaded.get("positions", {}), dict
                ):
                    raise ValueError("state file has an unexpected structure")
                if "external_flows" in loaded:
                    if "external_flow_ledger" not in loaded or not loaded["external_flow_ledger"]:
                        loaded["external_flow_ledger"] = loaded.pop("external_flows")
                    else:
                        loaded.pop("external_flows")
                self.data.update(loaded)
            except Exception as exc:
                # Fail closed: never silently discard persisted positions or risk state.
                raise RuntimeError(
                    f"Cannot safely load {DATA_FILE.name}; preserve the file and "
                    f"recover it before restarting: {exc}"
                ) from exc
        else:
            if LIVE:
                # Missing local state is not proof that the exchange has no open trades.
                # Require manual reconciliation before allowing any new entries.
                self.data["manual_pause"] = True
                self.data["recovery_required"] = {
                    "symbol": None,
                    "quantity": None,
                    "entry_order_id": None,
                    "reason": (
                        "Local state file was missing at startup. Reconcile exchange "
                        "balances, open orders, and trade history before enabling entries."
                    ),
                    "time": time.time(),
                }
            else:
                self.data["manual_pause"] = False
                self.data["recovery_required"] = None

    def _validate_finite_state(self) -> str | None:
        """Scan critical fields for non-finite numbers (NaN, Inf, or string 'nan').
        Returns error reason string if invalid, None if clean."""
        def is_bad(val) -> bool:
            if val is None:
                return False
            if isinstance(val, (int, float)):
                return not math.isfinite(val)
            if isinstance(val, str):
                s = val.strip().lower()
                if s in ("nan", "inf", "-inf", "+inf"):
                    return True
                try:
                    f = float(s)
                    return not math.isfinite(f)
                except ValueError:
                    return False
            return False

        for field in ("last_account_equity_usdt", "net_external_flows_usdt", "day_start_equity", "week_start_equity", "peak_equity"):
            v = self.data.get(field)
            if is_bad(v):
                return f"non-finite value in field {field}: {v!r}"

        for idx, t in enumerate(self.data.get("trades", [])):
            if not isinstance(t, dict):
                continue
            for k in ("pnl", "gross_realized_pnl_usdt_estimate", "exit", "entry", "qty"):
                v = t.get(k)
                if is_bad(v):
                    return f"non-finite value in trade {idx} field {k}: {v!r}"

        for sym, p in self.data.get("positions", {}).items():
            if not isinstance(p, dict):
                continue
            for k in ("quantity", "entry_price", "stop_price", "target_price"):
                v = p.get(k)
                if is_bad(v):
                    return f"non-finite value in position {sym} field {k}: {v!r}"

        return None

    def save(self):
        with self.lock:
            err = self._validate_finite_state()
            if err:
                log.critical("State save refused: %s. Previous state file preserved.", err)
                return False

            try:
                payload = json.dumps(self.data, indent=2, allow_nan=False)
            except ValueError as ve:
                log.critical("State save refused by JSON serializer: %s. Previous state file preserved.", ve)
                return False

            if payload == self._last_saved_payload:
                return True  # Skip write if state has not changed
            tmp = DATA_FILE.with_name(f"{DATA_FILE.name}.tmp.{os.getpid()}")
            try:
                tmp.write_text(payload, encoding="utf-8")
                for attempt in range(8):
                    try:
                        tmp.replace(DATA_FILE)  # retried: Windows can briefly lock the file
                        self._last_saved_payload = payload
                        return True
                    except OSError as err:
                        if attempt == 7:
                            log.critical("Atomic state save (replace) failed after 8 retries (%s). Halting entries safely without direct-write.", err)
                            self.data["manual_pause"] = True
                            self.data["manual_pause_reason"] = "state_save_disk_failure"
                            try:
                                if tmp.exists():
                                    tmp.unlink()
                            except OSError:
                                pass
                            return False
                        time.sleep(0.04 * (1.5 ** attempt))
            except Exception as exc:
                log.critical("State save exception (%s). Halting entries safely.", exc)
                self.data["manual_pause"] = True
                self.data["manual_pause_reason"] = "state_save_exception"
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
                return False

    def event(self, message):
        with self.lock:
            self.data.setdefault("events", []).append(
                {"time": time.time(), "message": str(message)}
            )
            self.data["events"] = self.data["events"][-200:]
            self.save()

state = State()


def notify(message):
    log.info(message)
    state.event(message)
    if TG_TOKEN and TG_CHAT:
        try:
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
            body = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": message}).encode()
            req = urllib.request.Request(url, data=body, method="POST")
            urllib.request.urlopen(req, timeout=8).read()
        except Exception: log.warning("Telegram alert failed (details suppressed to protect the bot token).")


def notify_trade_dashboard_snapshot(event_label, symbol=None):
    """Send a compact dashboard-data snapshot to Telegram after a verified trade."""
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        data = state.data
        positions = data.get("positions", {})
        trades = data.get("trades", [])
        lines = [f"📊 DASHBOARD SNAPSHOT · {event_label}"]
        if symbol:
            lines.append(f"Symbol: {symbol}")
        if bot_instance is not None:
            try:
                balances = bot_instance.balances()
                equity = bot_instance.equity_usdt(balances)
                free_usdt = balances.get("USDT", (0.0, 0.0))[0]
                lines.append(f"Strategy equity: {equity:.2f} USDT | Free USDT: {free_usdt:.2f}")
            except Exception:
                lines.append("Account snapshot: temporarily unavailable; check dashboard")
        lines.append(f"Open positions: {len(positions)}")
        for sym, pos in list(positions.items())[:8]:
            lines.append(
                f"• {sym}: qty={pos.get('quantity','?')} entry={pos.get('entry_price','?')} "
                f"stop={pos.get('stop_price','?')} target={pos.get('target_price','?')} "
                f"OCO={pos.get('oco_order_list_id','?')}"
            )
        recent = next((t for t in reversed(trades) if symbol is None or t.get("symbol") == symbol), None)
        if recent:
            lines.append(
                f"Latest record: {recent.get('symbol','?')} {recent.get('status','?')} "
                f"qty={recent.get('qty', recent.get('exit_quantity','?'))} "
                f"entry={recent.get('entry','?')} exit={recent.get('exit','—')}"
            )
        lines.append(f"Manual pause: {bool(data.get('manual_pause'))} | Recovery required: {bool(data.get('recovery_required'))}")
        text = "\n".join(lines)
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        body = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text[:3900]}).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:
        log.warning("Telegram dashboard snapshot failed (details suppressed to protect the bot token).")


def floor_step(value: float, step: str) -> str:
    v, s = Decimal(str(value)), Decimal(step)
    if s <= 0: return format(v, "f")
    return format((v / s).to_integral_value(rounding=ROUND_DOWN) * s, "f")

def ceil_step(value: float, step: str) -> str:
    v, s = Decimal(str(value)), Decimal(step)
    if s <= 0: return format(v, "f")
    return format((v / s).to_integral_value(rounding=ROUND_UP) * s, "f")

def fmt_price(client: Binance, symbol: str, value: float, upward=False):
    f = client.filters[symbol]["PRICE_FILTER"]
    return ceil_step(value, f["tickSize"]) if upward else floor_step(value, f["tickSize"])

def fmt_qty(client: Binance, symbol: str, value: float):
    f = client.filters[symbol]["LOT_SIZE"]
    return floor_step(value, f["stepSize"])

def ema(values, period):
    if len(values) < period: return None
    k = 2 / (period + 1)
    out = sum(values[:period]) / period
    for v in values[period:]: out = v * k + out * (1-k)
    return out

def atr(candles, period=14):
    if len(candles) < period + 1: return None
    trs=[]
    for i in range(1, len(candles)):
        hi, lo, prev = float(candles[i][2]), float(candles[i][3]), float(candles[i-1][4])
        trs.append(max(hi-lo, abs(hi-prev), abs(lo-prev)))
    return sum(trs[-period:]) / period

def candle_closed(row):
    return int(row[6]) < int(time.time()*1000)

def closed_rows(rows): return [r for r in rows if candle_closed(r)]

def utc_keys():
    now = time.gmtime()
    day = time.strftime("%Y-%m-%d", now)
    week = f"{now.tm_year}-W{time.strftime('%W', now)}"
    return day, week

class SwingBot:
    def __init__(self):
        self.client = Binance()
        self.last_hour_close: dict[str, int] = {
            str(k): int(v) for k, v in state.data.get("last_hour_close", {}).items()
            if str(v).isdigit()
        }
        self.last_15m_close: dict[str, int] = {
            str(k): int(v) for k, v in state.data.get("last_15m_close", {}).items()
            if str(v).isdigit()
        }
        self.scan_index = 0
        self.last_reconcile = 0.0
        self.last_preflight = 0.0
        self.last_equity = None
        self.last_balances = {}
        self.last_refresh_time = None
        self._cached_btc_regime: tuple[bool, str] = (True, "Uninitialized")
        self._last_btc_regime_at: float = 0.0
        if state.data.get("recovery_required"):
            state.data["manual_pause"] = True
            state.save()
            log.critical("Unresolved recovery state found at startup; all new entries remain locked.")
        bl_info = f", Blacklisted={len(BLACKLISTED_SYMBOLS)} pairs" if BLACKLISTED_SYMBOLS else ""
        notify(
            f"All-coin Spot swing bot loaded. LIVE_TRADING={LIVE}; "
            f"EXIT_MODE={EXIT_MODE} (TP={TAKE_PROFIT_PCT*100:.2f}%, StopGuard={MIN_STOP_PCT*100:.1f}%-{MAX_STOP_PCT*100:.1f}%, HoldHours={MAX_HOLD_HOURS}{bl_info}); "
            f"scan_all_usdt_spot={SCAN_ALL_USDT_SPOT}; initial scan universe={len(getattr(self.client, 'symbols', []))} pairs."
        )

    def balances(self):
        if not LIVE:
            initial_capital = 1000.0
            paper_realized = 0.0
            for t in state.data.get("trades", []):
                if not isinstance(t, dict):
                    continue
                if t.get("status") in TERMINAL_TRADE_STATUSES:
                    raw_pnl = t.get("pnl")
                    if raw_pnl is None:
                        raw_pnl = t.get("gross_realized_pnl_usdt_estimate", 0.0)
                    try:
                        val = float(raw_pnl)
                        if math.isfinite(val):
                            paper_realized += val
                    except (ValueError, TypeError):
                        pass

            net_flows = 0.0
            try:
                flows_val = float(state.data.get("net_external_flows_usdt", 0.0) or 0.0)
                if math.isfinite(flows_val):
                    net_flows = flows_val
            except (ValueError, TypeError):
                net_flows = 0.0

            total_cash = initial_capital + paper_realized + net_flows
            if not math.isfinite(total_cash):
                total_cash = initial_capital

            open_cost = 0.0
            for p in state.data.get("positions", {}).values():
                if not isinstance(p, dict):
                    continue
                try:
                    entry = float(p.get("entry_price", 0.0) or 0.0)
                    qty = float(p.get("quantity", 0.0) or 0.0)
                    if math.isfinite(entry) and math.isfinite(qty) and entry > 0 and qty > 0:
                        open_cost += entry * qty
                except (ValueError, TypeError):
                    continue

            if not math.isfinite(open_cost):
                open_cost = 0.0

            free_usdt = max(0.0, total_cash - open_cost)
            bals = {"USDT": (free_usdt, open_cost)}
            for symbol, p in state.data.get("positions", {}).items():
                if isinstance(p, dict) and symbol.endswith("USDT"):
                    asset = symbol[:-4]
                    try:
                        q = float(p.get("quantity", 0.0) or 0.0)
                        if math.isfinite(q):
                            bals[asset] = (q, 0.0)
                    except (ValueError, TypeError):
                        bals[asset] = (0.0, 0.0)
            return bals

        acc = self.client.account()
        return {x["asset"]: (float(x["free"]), float(x["locked"])) for x in acc.get("balances", []) if float(x["free"])+float(x["locked"]) > 0}

    def equity_usdt(self, bals=None):
        if not LIVE:
            initial_capital = 1000.0
            paper_realized = 0.0
            for t in state.data.get("trades", []):
                if not isinstance(t, dict):
                    continue
                if t.get("status") in TERMINAL_TRADE_STATUSES:
                    raw_pnl = t.get("pnl")
                    if raw_pnl is None:
                        raw_pnl = t.get("gross_realized_pnl_usdt_estimate", 0.0)
                    try:
                        val = float(raw_pnl)
                        if math.isfinite(val):
                            paper_realized += val
                    except (ValueError, TypeError):
                        pass

            net_flows = 0.0
            try:
                flows_val = float(state.data.get("net_external_flows_usdt", 0.0) or 0.0)
                if math.isfinite(flows_val):
                    net_flows = flows_val
            except (ValueError, TypeError):
                net_flows = 0.0

            cash = initial_capital + paper_realized + net_flows
            if not math.isfinite(cash):
                cash = initial_capital

            unrealized = 0.0
            for symbol, p in state.data.get("positions", {}).items():
                if not isinstance(p, dict):
                    continue
                try:
                    qty = float(p.get("quantity", 0.0) or 0.0)
                    entry = float(p.get("entry_price", 0.0) or 0.0)
                except (ValueError, TypeError):
                    continue
                if not (math.isfinite(qty) and math.isfinite(entry) and qty > 0 and entry > 0):
                    continue

                mark = entry
                if hasattr(self, "client") and self.client is not None:
                    try:
                        p_val = self.client.price(symbol)
                        if p_val is not None:
                            p_float = float(p_val)
                            if math.isfinite(p_float) and p_float > 0:
                                mark = p_float
                    except Exception:
                        if hasattr(self.client, "latest_prices") and symbol in self.client.latest_prices:
                            try:
                                p_cand = float(self.client.latest_prices[symbol])
                                if math.isfinite(p_cand) and p_cand > 0:
                                    mark = p_cand
                            except (ValueError, TypeError):
                                pass

                diff = mark - entry
                if math.isfinite(diff):
                    pos_pnl = qty * diff
                    if math.isfinite(pos_pnl):
                        unrealized += pos_pnl

            total = cash + unrealized
            if not math.isfinite(total) or total <= 0:
                total = initial_capital + net_flows
            return round(total, 4)
        if bals is None:
            bals = self.balances()
        total = sum(bals.get("USDT", (0, 0)))
        if not math.isfinite(total) or total < 0:
            raise RuntimeError("Cannot safely value invalid USDT balance.")
        # EXCLUDED_SYMBOLS represents fee-reserve assets (BNB by default),
        # not strategy capital. Ignore their market value in strategy equity so
        # routine BNB price changes or fee deductions do not create false
        # unrecorded-account-change locks. The balances remain in Spot for fees.
        fee_reserve_assets = {
            excluded[:-4] for excluded in EXCLUDED_SYMBOLS if excluded.endswith("USDT")
        }
        for asset, pair in bals.items():
            if asset == "USDT" or asset in fee_reserve_assets:
                continue
            amount = sum(pair)
            if amount <= 0:
                continue
            symbol = f"{asset}USDT"
            if symbol not in self.client.filters:
                raise RuntimeError(
                    f"Cannot safely value account asset {asset}: no eligible USDT Spot pair. "
                    "Reconcile account assets before trading."
                )
            price = float(self.client.price(symbol))
            if not math.isfinite(price) or price <= 0:
                raise RuntimeError(f"Cannot safely value {asset}; refusing to calculate equity.")
            total += amount * price
        if not math.isfinite(total) or total <= 0:
            raise RuntimeError("Calculated USDT-equivalent equity is invalid; refusing to trade.")
        return total

    def btc_regime(self) -> tuple[bool, str]:
        """Check if BTC is above its 4H 50-EMA (Macro Risk-On vs Risk-Off). Fails closed on any error."""
        now = time.time()
        if now - self._last_btc_regime_at < 300 and self._last_btc_regime_at > 0:
            return self._cached_btc_regime
        try:
            klines = closed_rows(self.client.klines("BTCUSDT", "4h", 60))
            if len(klines) < 52:
                self._cached_btc_regime = (False, "Insufficient BTC klines (fail-closed)")
                self._last_btc_regime_at = now
                return False, "Insufficient BTC klines (fail-closed)"
            closes = [float(x[4]) for x in klines]
            e50 = ema(closes, 50)
            if e50 is None:
                self._cached_btc_regime = (False, "BTC EMA calculation unavailable (fail-closed)")
                self._last_btc_regime_at = now
                return False, "BTC EMA calculation unavailable (fail-closed)"
            last_close = closes[-1]
            healthy = last_close > e50
            status = f"HEALTHY (BTC {last_close:.1f} > 50-EMA {e50:.1f})" if healthy else f"RISK-OFF (BTC {last_close:.1f} <= 50-EMA {e50:.1f})"
            self._cached_btc_regime = (healthy, status)
            self._last_btc_regime_at = now
            state.event(f"BTC Regime check: {status}")
            return healthy, status
        except Exception as e:
            log.warning("Could not check BTC regime: %s; failing closed", e)
            self._cached_btc_regime = (False, f"BTC check error: {e} (fail-closed)")
            self._last_btc_regime_at = now
            return False, f"BTC check error: {e} (fail-closed)"

    def set_flat_baseline(self) -> bool:
        """Record the post-exit flat equity baseline, flow-adjusted so recorded deposits do not relock entries.
        Network balance/equity calls occur outside the state lock to prevent blocking."""
        try:
            bals = self.balances()
            eq = float(self.equity_usdt(bals))
            if not math.isfinite(eq) or eq <= 0:
                raise ValueError(f"invalid equity {eq}")
            with state.lock:
                raw_flows = state.data.get("net_external_flows_usdt", 0) or 0
                try:
                    flows = float(raw_flows)
                    if not math.isfinite(flows):
                        flows = 0.0
                except (ValueError, TypeError):
                    flows = 0.0
                baseline = round(eq - flows, 4)
                if not math.isfinite(baseline) or baseline <= 0:
                    baseline = round(eq, 4)
                state.data["last_account_equity_usdt"] = baseline
                state.save()
            return True
        except Exception as exc:
            with state.lock:
                state.data["last_account_equity_usdt"] = None
                state.data["manual_pause"] = True
                state.data["manual_pause_reason"] = "post_exit_rebaseline_failed"
                state.save()
            notify(f"CRITICAL: could not read equity after exit; entries locked: {exc}")
            return False

    def check_unrecorded_flat_account_change(self, equity):
        """Lock if account equity changes while flat; deposits/withdrawals must be recorded explicitly."""
        d = state.data
        if d.get("account_change_requires_review"):
            d["manual_pause"] = True
            if not d.get("manual_pause_reason"):
                d["manual_pause_reason"] = "unrecorded_account_change"
            return False
        if d.get("positions"):
            return True
        previous = d.get("last_account_equity_usdt")
        if previous is None:
            # A missing baseline while flat must lock, never silently absorb changes
            d["manual_pause"] = True
            d["manual_pause_reason"] = "baseline_missing"
            log.critical("Account equity check failed: last_account_equity_usdt baseline is missing while flat.")
            notify("CRITICAL: flat equity baseline is missing; entries locked.")
            return False
        try:
            previous = float(previous)
        except (ValueError, TypeError):
            previous = None
        if previous is None or not math.isfinite(previous):
            d["manual_pause"] = True
            d["manual_pause_reason"] = "baseline_missing"
            log.critical("Account equity check failed: last_account_equity_usdt baseline is not finite.")
            notify("CRITICAL: flat equity baseline is missing; entries locked.")
            return False

        try:
            flows = float(d.get("net_external_flows_usdt", 0) or 0)
            if not math.isfinite(flows):
                flows = 0.0
        except (ValueError, TypeError):
            flows = 0.0

        if not math.isfinite(float(equity)):
            log.critical("check_unrecorded_flat_account_change called with non-finite equity: %s", equity)
            return False

        flow_adjusted_equity = float(equity) - flows
        delta = flow_adjusted_equity - previous
        if abs(delta) > EQUITY_CHANGE_REVIEW_THRESHOLD_USDT:
            d["account_change_requires_review"] = {
                "previous_equity_usdt": previous,
                "current_equity_usdt": float(equity),
                "flow_adjusted_equity_usdt": flow_adjusted_equity,
                "delta_usdt": delta,
                "reason": (
                    "Account equity changed while no bot position was open. This may be a deposit, "
                    "withdrawal, or manual trade. Record/reconcile the external activity before resuming."
                ),
                "time": time.time(),
            }
            d["manual_pause"] = True
            if not d.get("manual_pause_reason"):
                d["manual_pause_reason"] = "unrecorded_account_change"
            state.save()
            log.critical(
                "Account equity changed by %.4f USDT while flat; new entries locked for flow reconciliation.",
                delta,
            )
            notify(
                "CRITICAL: account equity changed while the bot was flat. Trading is paused. "
                "Use record_external_flow.py only for a confirmed USDT deposit/withdrawal; investigate manual trades separately."
            )
            return False
        return True

    def risk_status(self, equity):
        day, week = utc_keys()
        d = state.data
        if d.get("account_change_requires_review"):
            d["manual_pause"] = True
            if not d.get("manual_pause_reason"):
                d["manual_pause_reason"] = "unrecorded_account_change"
            state.save()
            return False, "unreconciled account equity change"

        net_external_flows = float(d.get("net_external_flows_usdt", 0.0) or 0.0)
        risk_equity = flow_adjusted_equity(equity, net_external_flows)
        if not math.isfinite(risk_equity) or risk_equity <= 0:
            d["manual_pause"] = True
            d["manual_pause_reason"] = "invalid_flow_adjusted_equity"
            state.save()
            return False, "flow-adjusted equity is invalid"

        if d.get("day_key") != day:
            d["day_key"], d["day_start_equity"] = day, risk_equity
        if d.get("week_key") != week:
            d["week_key"], d["week_start_equity"] = week, risk_equity
        if d.get("peak_equity") is None:
            d["peak_equity"] = risk_equity
        else:
            d["peak_equity"] = max(float(d["peak_equity"]), risk_equity)

        if (
            d.get("day_start_equity")
            and risk_equity <= float(d["day_start_equity"]) * (1 - DAILY_LOSS_PCT)
            and d.get("daily_loss_triggered_day") != day
        ):
            d["paused_until"] = max(float(d.get("paused_until", 0)), time.time() + 24 * 3600)
            d["daily_loss_triggered_day"] = day
            log.warning("Daily loss threshold reached; entries locked for 24 hours")

        if d.get("week_start_equity") and risk_equity <= float(d["week_start_equity"]) * (1 - WEEKLY_LOSS_PCT):
            d["manual_pause"] = True
            d["manual_pause_reason"] = "weekly_loss_limit"
        if d.get("peak_equity") and risk_equity <= float(d["peak_equity"]) * (1 - DRAWDOWN_PCT):
            d["manual_pause"] = True
            d["manual_pause_reason"] = "max_drawdown_limit"
        state.save()
        if d.get("manual_pause"):
            return False, f"manual pause: {d.get('manual_pause_reason') or 'risk or manual review'}"
        if time.time() < float(d.get("paused_until", 0)):
            return False, "daily loss lockout"
        return True, "ok"

    def total_exposure(self):
        # Fail closed: unknown prices must never make exposure appear smaller.
        total = 0.0
        for position in state.data["positions"].values():
            symbol = position.get("symbol")
            quantity = float(position.get("quantity", 0))
            if not symbol or not math.isfinite(quantity) or quantity < 0:
                raise RuntimeError("Cannot safely calculate total exposure from invalid position state.")
            price = float(self.client.price(symbol))
            if not math.isfinite(price) or price <= 0:
                raise RuntimeError(f"Cannot safely value {symbol}; refusing to calculate exposure.")
            total += quantity * price
        return total

    def _update_15m_pattern(self, symbol, candles):
        """Track a closed-candle breakout/retest pattern on 15m candles.

        A confirmed 15m pattern remains valid for at most four 15m candles (one hour).
        It is a confirmation filter only; it cannot create a trade without the existing
        4h trend and 1h breakout/retest setup.
        """
        if len(candles) < 30:
            return
        latest = candles[-1]
        close_time = int(latest[6])
        if self.last_15m_close.get(symbol) == close_time:
            return

        highs = [float(x[2]) for x in candles[-21:-1]]
        level = max(highs)
        volumes = [float(x[5]) for x in candles[-21:-1]]
        avgvol = sum(volumes) / len(volumes) if volumes else 0.0
        volume = float(latest[5])
        close = float(latest[4])
        a = atr(candles[-30:], 14)
        if a is None or avgvol <= 0:
            return

        with state.lock:
            self.last_15m_close[symbol] = close_time
            state.data.setdefault("last_15m_close", {})[symbol] = close_time

            # Expire stale confirmations before updating the current closed candle.
            confirmed = state.data.setdefault("confirmed_15m_patterns", {})
            if symbol in confirmed and close_time - int(confirmed[symbol]) > 4 * 15 * 60 * 1000:
                confirmed.pop(symbol, None)

            setups = state.data.setdefault("setups_15m", {})
            setup = setups.get(symbol)
            if setup is None:
                if close > level and volume >= 1.5 * avgvol:
                    setups[symbol] = {
                        "level": level,
                        "breakout_close_time": close_time,
                        "bars_waited": 0,
                        "swing_low": float(latest[3]),
                    }
                    # A newer breakout invalidates an older confirmation.
                    confirmed.pop(symbol, None)
                    log.info("%s: 15m breakout detected at %.8g; waiting for retest", symbol, level)
                    state.event(f"{symbol}: 15m breakout detected at {level:.8g}; waiting for retest")
                return

            # Count only newly closed 15m candles (guarded above).
            setup["bars_waited"] = int(setup.get("bars_waited", 0)) + 1
            setup["swing_low"] = min(float(setup.get("swing_low", latest[3])), float(latest[3]))
            setup_level = float(setup["level"])
            near = float(latest[3]) <= setup_level + 0.25 * a and float(latest[3]) >= setup_level - 0.75 * a
            confirms = near and close > setup_level and close > float(candles[-2][2])
            if int(setup["bars_waited"]) > 6 or close < setup_level - 0.75 * a:
                setups.pop(symbol, None)
                log.info("%s: 15m breakout/retest setup expired", symbol)
                state.event(f"{symbol}: 15m breakout/retest setup expired")
            elif confirms:
                confirmed[symbol] = close_time
                setups.pop(symbol, None)
                log.info("%s: 15m breakout/retest confirmed; eligible as 1h entry filter", symbol)
                state.save()
                notify(f"{symbol}: 15m breakout/retest confirmed; waiting for 1h confirmation")

    def signal_for(self, symbol):
        h4 = closed_rows(self.client.klines(symbol, "4h", 220))
        h1 = closed_rows(self.client.klines(symbol, "1h", 100))
        m15 = closed_rows(self.client.klines(symbol, "15m", 100))
        if len(h4) < 205 or len(h1) < 30 or len(m15) < 30:
            return None

        # Update the faster-timeframe pattern only if ENABLE_15M_FILTER is active.
        if ENABLE_15M_FILTER:
            self._update_15m_pattern(symbol, m15)

        h4_closes = [float(x[4]) for x in h4]
        e_now = ema(h4_closes, 200)
        e_then = ema(h4_closes[:-5], 200)
        trend_ok = (e_now is not None and e_then is not None and h4_closes[-1] > e_now and e_now > e_then)
        if not trend_ok:
            return None

        latest = h1[-1]
        close_time = int(latest[6])
        if self.last_hour_close.get(symbol) == close_time:
            return None

        highs = [float(x[2]) for x in h1[-21:-1]]
        level = max(highs)
        volumes = [float(x[5]) for x in h1[-21:-1]]
        avgvol = sum(volumes) / len(volumes) if volumes else 0.0
        vol = float(latest[5])
        cclose = float(latest[4])
        a = atr(h1[-30:], 14)
        if a is None or avgvol <= 0:
            return None

        # -------------------------------------------------------------
        # Option 1: Stateless Retrospective Retest Engine
        # Evaluates completed breakout + bounce sequences over the past 1-4
        # closed candles. Zero waiting states, zero tickets in memory.
        # -------------------------------------------------------------
        is_signal = False
        entry_type = None
        lev = level
        bars_waited = 0
        swing_low = float(latest[3])

        # Inspect past 1 to 4 closed candles for a completed breakout and bounce
        for k in range(1, 5):
            bo_idx = -1 - k
            if abs(bo_idx) > len(h1):
                continue
            bo_candle = h1[bo_idx]
            bo_highs = [float(x[2]) for x in h1[bo_idx-20:bo_idx]]
            bo_level = max(bo_highs)
            bo_vols = [float(x[5]) for x in h1[bo_idx-20:bo_idx]]
            bo_avgvol = sum(bo_vols) / len(bo_vols) if bo_vols else 0.0
            bo_vol = float(bo_candle[5])
            bo_close = float(bo_candle[4])
            if bo_close > bo_level and bo_vol >= 1.5 * bo_avgvol and bo_avgvol > 0:
                near = float(latest[3]) <= bo_level + 0.25 * a and float(latest[3]) >= bo_level - 0.75 * a
                if near and cclose > bo_level and cclose > float(h1[-2][2]):
                    is_signal = True
                    entry_type = "RETEST_BOUNCE"
                    lev = bo_level
                    bars_waited = k
                    swing_low = min(float(h1[x][3]) for x in range(len(h1) + bo_idx, len(h1)))
                    break

        # Log observation when a new breakout candle closes (informational only; zero waiting state)
        if not is_signal and cclose > level and vol >= 1.5 * avgvol and avgvol > 0:
            log.info("%s: 1h resistance breakout observed at %.8g (vol=%.1fx); retrospective engine will verify bounce", symbol, level, vol / avgvol)

        if not is_signal:
            with state.lock:
                self.last_hour_close[symbol] = close_time
                state.data.setdefault("last_hour_close", {})[symbol] = close_time
                state.save()
            return None

        # Market-Context Gates (FINAL_PLAN.md Steps 3 & 4)
        btc_healthy, btc_status = self.btc_regime()
        if ENABLE_BTC_REGIME_GATE and not btc_healthy:
            with state.lock:
                self.last_hour_close[symbol] = close_time
                state.data.setdefault("last_hour_close", {})[symbol] = close_time
                state.save()
            log.info("%s: %s detected but skipped; %s", symbol, entry_type, btc_status)
            return None

        rs = self.client.relative_strength.get(symbol, 0.0)
        if ENABLE_RS_FILTER and symbol != "BTCUSDT" and rs <= 0.0:
            with state.lock:
                self.last_hour_close[symbol] = close_time
                state.data.setdefault("last_hour_close", {})[symbol] = close_time
                state.save()
            log.info("%s: %s detected but skipped; weak relative strength vs BTC (%+.2f%%)", symbol, entry_type, rs)
            return None

        stop = swing_low - 0.2 * a
        entry = cclose
        dist = entry - stop
        with state.lock:
            self.last_hour_close[symbol] = close_time
            state.data.setdefault("last_hour_close", {})[symbol] = close_time
            state.save()

        # Enforces configurable stop distance guard (e.g. 1.5% - 4.0%)
        if dist <= 0 or (dist / entry) < MIN_STOP_PCT or (dist / entry) > MAX_STOP_PCT:
            log.info(
                "%s: %s skipped; stop distance %.2f%% outside guard [%.2f%%, %.2f%%]",
                symbol, entry_type, (dist / entry) * 100, MIN_STOP_PCT * 100, MAX_STOP_PCT * 100
            )
            return None

        target = compute_target_price(entry, stop)
        if target <= entry:
            return None

        # 100-Point Transparent Scorecard (FINAL_PLAN.md Step 5)
        s_btc = 25 if btc_healthy else 0
        s_rs = 25 if rs > 0.0 else (10 if rs > -3.0 else 0)
        s_vol = 20 if vol >= 2.0 * avgvol else 15
        s_ret = 20 if bars_waited <= 3 else 15
        s_risk = 10 if (dist / entry) <= 0.025 else 5
        score = s_btc + s_rs + s_vol + s_ret + s_risk
        score_desc = f"Score {score}/100 [BTC: {s_btc}/25, RS: {s_rs}/25, Vol: {s_vol}/20, Retest: {s_ret}/20, Risk: {s_risk}/10]"

        if MIN_SCORECARD_POINTS > 0 and score < MIN_SCORECARD_POINTS:
            log.info("%s: %s skipped; scorecard %d/100 below MIN_SCORECARD_POINTS (%d)", symbol, entry_type, score, MIN_SCORECARD_POINTS)
            return None

        log.info(">>> VALID ENTRY SIGNAL FOR %s (%s): Entry=%.8g Stop=%.8g Target=%.8g %s", symbol, entry_type, entry, stop, target, score_desc)
        notify(f"{symbol}: {entry_type} confirmed at {entry:.8g}; target={target:.8g}, stop={stop:.8g} ({score_desc})")

        return {
            "symbol": symbol,
            "entry": entry,
            "stop": stop,
            "target": target,
            "exit_mode": EXIT_MODE,
            "level": lev,
            "close_time": close_time,
            "confirmation_15m_close_time": 0,
            "score": score,
            "score_desc": score_desc,
            "btc_status": btc_status,
            "relative_strength": rs,
        }

    def _base_commission(self, symbol: str, order_id: int, base_asset: str) -> Decimal:
        """Fetch signed trade executions to find any commission taken directly in the bought base asset.
        Fails closed on any error to avoid submitting an oversized OCO."""
        trades = self.client.request(
            "GET", "/api/v3/myTrades",
            {"symbol": symbol, "orderId": order_id}, signed=True
        )
        if not isinstance(trades, list):
            raise RuntimeError(f"Unexpected /api/v3/myTrades response for {symbol} order {order_id}: {trades}")
        fee = Decimal("0")
        for t in trades:
            if str(t.get("commissionAsset", "")).upper() == base_asset.upper():
                fee += Decimal(str(t.get("commission", "0")))
        return fee

    def _direct_market_close_no_oco(self, symbol: str, sell_qty: Decimal, avg_entry: float, oid: int, stop_s: str, target_s: str, reason: str):
        """Immediately market-sell a newly bought position that breached target/stop before OCO placement.
        No exchange OCO exists yet, so no OCO cancellation is attempted."""
        try:
            sell_order = self.client.order({
                "symbol": symbol,
                "side": "SELL",
                "type": "MARKET",
                "quantity": format(sell_qty, "f"),
                "newOrderRespType": "FULL"
            })
        except Exception as exc:
            log.critical("%s: direct market sell failed after immediate target/stop breach: %s", symbol, exc)
            with state.lock:
                state.data["manual_pause"] = True
                state.data["recovery_required"] = {
                    "symbol": symbol,
                    "quantity": format(sell_qty, "f"),
                    "entry_order_id": oid,
                    "reason": f"Direct market sell failed after immediate {reason} breach ({exc}); manual reconciliation required.",
                    "time": time.time()
                }
                state.save()
            notify(f"CRITICAL: {symbol} direct market sell failed: {exc}. Recovery locked.")
            return

        executed = Decimal(str(sell_order.get("executedQty", "0")))
        quote = Decimal(str(sell_order.get("cummulativeQuoteQty", "0")))
        if sell_order.get("status") != "FILLED" or executed != sell_qty or quote <= 0:
            with state.lock:
                state.data["manual_pause"] = True
                state.data["recovery_required"] = {
                    "symbol": symbol,
                    "quantity": format(sell_qty, "f"),
                    "entry_order_id": oid,
                    "reason": f"Direct market sell partial/unconfirmed fill (status={sell_order.get('status')}, executed={executed}); manual reconciliation required.",
                    "time": time.time()
                }
                state.save()
            notify(f"CRITICAL: {symbol} direct market sell was not a full fill; recovery locked.")
            return

        avg_exit = float(quote / executed)
        gross_pnl = float(quote - Decimal(str(avg_entry)) * executed)
        status_str = "CLOSED_TARGET" if reason == "TARGET" else "CLOSED_STOP"

        with state.lock:
            state.data["positions"].pop(symbol, None)
            rec_lock = state.data.get("recovery_required")
            if isinstance(rec_lock, dict) and rec_lock.get("symbol") == symbol:
                state.data["recovery_required"] = None
                state.data["manual_pause"] = False
            state.data["trades"].append({
                "time": time.time(),
                "symbol": symbol,
                "status": status_str,
                "entry": avg_entry,
                "stop": float(stop_s),
                "target": float(target_s),
                "qty": format(sell_qty, "f"),
                "order_id": oid,
                "exit_order_id": int(sell_order.get("orderId", 0)),
                "exit": avg_exit,
                "exit_time": time.time(),
                "exit_quantity": format(executed, "f"),
                "gross_realized_pnl_usdt_estimate": round(gross_pnl, 4),
                "exit_reason": f"IMMEDIATE_{reason}_BREACH_BEFORE_OCO",
                "pnl_note": "Direct market exit after immediate breach; no OCO submitted."
            })
            state.save()

        if not state.data.get("positions"):
            self.set_flat_baseline()

        msg = f"{symbol}: direct market close filled immediately after BUY: exit={avg_exit:.8g}, gross PnL={gross_pnl:+.4f} USDT, order={sell_order.get('orderId')}"
        notify(msg)
        return {"ok": True, "symbol": symbol, "quantity": format(executed, "f"), "average_exit": avg_exit, "gross_pnl_usdt_estimate": gross_pnl}

    def open_trade(self, sig, equity, balances):
        # Recovery is a hard lock independent of the manual-pause flag.
        if state.data.get("recovery_required"):
            log.critical("Entry blocked: unresolved recovery_required state exists.")
            return
        symbol = sig["symbol"]
        if symbol in state.data.get("positions", {}):
            return
        # Refuse if any trade for that symbol is still OPEN in trade ledger
        if any(t.get("symbol") == symbol and t.get("status") == "OPEN" for t in state.data.get("trades", [])):
            log.warning("%s: an OPEN trade already exists in state ledger; refusing duplicate entry.", symbol)
            return
        if len(state.data.get("positions", {})) >= MAX_POSITIONS:
            return
        ok, why = self.risk_status(equity)
        if not ok: notify(f"Entries paused: {why}"); return
        free_usdt = balances.get("USDT", (0,0))[0]
        exposure = self.total_exposure()
        if free_usdt < equity*RESERVE_PCT: return
        if exposure >= equity*MAX_EXPOSURE_PCT: return
        entry=float(sig["entry"]); stop=float(sig["stop"]); target=float(sig["target"])
        # Use Decimal arithmetic for sizing so binary-float noise cannot push
        # an exact LOT_SIZE boundary just below the boundary before floor_step().
        equity_d = Decimal(str(equity))
        entry_d = Decimal(str(entry))
        stop_d = Decimal(str(stop))
        exposure_d = Decimal(str(exposure))
        free_usdt_d = Decimal(str(free_usdt))
        risk_budget_d = equity_d * Decimal(str(RISK_PCT)) * Decimal("0.80")
        risk_gap_d = entry_d - stop_d
        if risk_gap_d <= 0 or entry_d <= 0:
            log.warning("%s: invalid entry/stop values; skipping trade", symbol)
            return
        qty_by_risk = risk_budget_d / risk_gap_d
        qty_by_pos = TRADE_NOTIONAL_USDT / entry_d
        qty_by_exposure = max(
            Decimal("0"),
            equity_d * Decimal(str(MAX_EXPOSURE_PCT)) - exposure_d,
        ) / entry_d
        qty_by_cash = max(
            Decimal("0"),
            free_usdt_d - equity_d * Decimal(str(RESERVE_PCT)),
        ) / entry_d
        qty = min(qty_by_risk, qty_by_pos, qty_by_exposure, qty_by_cash)
        qtys = fmt_qty(self.client, symbol, qty)
        if float(qtys)<=0: return
        filters=self.client.filters[symbol]
        notional=float(qtys)*entry
        min_notional=float(filters.get("NOTIONAL", filters.get("MIN_NOTIONAL",{})).get("minNotional", "0"))
        if notional < min_notional: notify(f"{symbol}: skipped; order below exchange minimum notional"); return

        # Stale-Price Guard: fetch fresh real-time execution price right before BUY
        try:
            fresh_px = self.client.price(symbol)
        except Exception as e:
            log.warning("%s: failed to fetch fresh price (%s); skipping entry", symbol, e)
            return

        if fresh_px <= stop:
            log.warning("%s: fresh price (%.8g) is at or below stop (%.8g); rejecting entry", symbol, fresh_px, stop)
            notify(f"{symbol}: entry rejected; fresh price already at or below stop level")
            return
        if fresh_px >= target:
            log.warning("%s: fresh price (%.8g) already reached target (%.8g); rejecting entry", symbol, fresh_px, target)
            notify(f"{symbol}: entry rejected; fresh price already reached target")
            return
        drift = abs(fresh_px - entry) / entry
        if drift > 0.008:
            log.warning("%s: fresh price (%.8g) drifted %.2f%% from signal entry (%.8g); rejecting stale trade", symbol, fresh_px, drift * 100, entry)
            notify(f"{symbol}: entry rejected; price drifted {drift*100:.2f}% from setup close")
            return

        if not LIVE:
            # Paper fill mirrors the live path: slippage on entry, stop at least 0.1% below the
            # fill, and target recomputed from actual paper fill according to EXIT_MODE.
            fill = fresh_px * (1 + PAPER_SLIPPAGE)
            p_stop = min(stop, fill * (1 - 0.001))
            p_target = compute_target_price(fill, p_stop)
            score_text = f" | {sig.get('score_desc')}" if sig.get("score_desc") else ""
            notify(f"PAPER TRADE OPENED ({EXIT_MODE}): BUY {symbol} qty={qtys}, fill~{fill:.8g}, stop~{p_stop:.8g}, target~{p_target:.8g}{score_text}")
            
            trade_id = f"paper_{symbol}_{int(time.time() * 1000)}"
            now_ms = int(time.time() * 1000)
            open_candle_close_ms = (now_ms // (15 * 60 * 1000) + 1) * (15 * 60 * 1000) - 1

            with state.lock:
                state.data["trades"].append({
                    "id": trade_id,
                    "time": time.time(),
                    "symbol": symbol,
                    "status": "OPEN",
                    "entry": fill,
                    "stop": p_stop,
                    "target": p_target,
                    "exit_mode": EXIT_MODE,
                    "qty": qtys,
                    "score": sig.get("score"),
                    "score_desc": sig.get("score_desc"),
                    "is_paper": True,
                })
                state.data["positions"][symbol] = {
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "quantity": qtys,
                    "entry_price": fill,
                    "stop_price": p_stop,
                    "target_price": p_target,
                    "exit_mode": EXIT_MODE,
                    "opened_at": time.time(),
                    "open_candle_close_time": open_candle_close_ms,
                    "paper_checked_close_time": open_candle_close_ms,
                    "is_paper": True,
                    "score": sig.get("score"),
                }
                state.save()
            return
        limit_price=fmt_price(self.client,symbol,fresh_px*(1+MAX_SLIPPAGE),upward=True)
        # Write an intent before the network call. If the process crashes or the
        # response is lost, restart sees the recovery lock instead of placing another BUY.
        with state.lock:
            state.data["manual_pause"] = True
            state.data["recovery_required"] = {
                "symbol": symbol, "quantity": qtys, "entry_order_id": None,
                "reason": "BUY submission in progress/uncertain; reconcile Binance orders, balances, and trade history",
                "time": time.time()
            }
            state.save()
        try:
            order=self.client.order({"symbol":symbol,"side":"BUY","type":"LIMIT","timeInForce":"GTC","quantity":qtys,"price":limit_price,"newOrderRespType":"FULL"})
        except Exception as e:
            # A timeout/error can happen after Binance accepted the BUY. Never retry blindly.
            # Keep the pre-submission recovery lock until manually reconciled.
            notify(
                f"CRITICAL: {symbol} BUY submission outcome is uncertain: {e}. "
                "New entries paused. Check Binance Spot open orders, balances, and trade history. "
                "Do not submit another BUY until reconciled."
            )
            return
        oid=int(order["orderId"])
        with state.lock:
            state.data["recovery_required"]["entry_order_id"] = oid
            state.data["recovery_required"]["reason"] = "BUY accepted; tracking order until terminal and protective OCO is confirmed"
            state.save()
        notify(f"{symbol}: buy order submitted {oid}; waiting up to {ENTRY_ORDER_TIMEOUT_SECONDS} seconds")
        deadline=time.time()+ENTRY_ORDER_TIMEOUT_SECONDS
        latest=order
        terminal_statuses = ("FILLED", "CANCELED", "REJECTED", "EXPIRED")
        # Once a BUY is accepted, any status-query/cancel failure can leave a live
        # order or partial fill on Binance. Fail closed and require reconciliation.
        try:
            # If the submission response already reports a terminal status, do not wait
            # or make an unnecessary follow-up request.
            while (time.time() < deadline and not stop_event.is_set()
                   and latest.get("status") not in terminal_statuses):
                time.sleep(2)
                latest = self.client.query_order(symbol, oid)
            if latest.get("status") not in terminal_statuses:
                self.client.cancel_order(symbol, oid)
                latest = self.client.query_order(symbol, oid)
            if latest.get("status") not in terminal_statuses:
                raise RuntimeError(f"BUY order {oid} status still not terminal after cancellation attempt")
        except Exception as e:
            with state.lock:
                state.data["manual_pause"] = True
                state.data["recovery_required"] = {
                    "symbol": symbol, "quantity": qtys, "entry_order_id": oid,
                    "reason": "BUY accepted but status/cancel outcome uncertain; verify order status, open orders, balances, and trade history",
                    "time": time.time()
                }
                state.save()
            notify(
                f"CRITICAL: {symbol} BUY order {oid} was accepted but status/cancel outcome is uncertain: {e}. "
                "New entries paused. Verify Binance order status, open orders, balances, and trade history. "
                "Do not submit another BUY until reconciled."
            )
            return
        filled_decimal = Decimal(str(latest.get("executedQty", "0")))
        filled = float(filled_decimal)
        quote = float(latest.get("cummulativeQuoteQty", 0))
        if filled_decimal <= 0:
            with state.lock:
                state.data["recovery_required"] = None
                state.data["manual_pause"] = False
                state.save()
            notify(f"{symbol}: entry not filled; order cancelled/skipped")
            return
        avg = quote / filled if quote else entry
        # Account for base-asset commission (e.g. BTC fee on BTCUSDT)
        base_asset = symbol[:-4] if symbol.endswith("USDT") else symbol
        try:
            base_fee = self._base_commission(symbol, oid, base_asset)
        except Exception as fee_err:
            with state.lock:
                state.data["manual_pause"] = True
                state.data["recovery_required"] = {
                    "symbol": symbol, "quantity": format(filled_decimal, "f"), "entry_order_id": oid,
                    "reason": f"BUY filled ({oid}) but base-asset commission check failed ({fee_err}); verify trades on Binance before resuming.",
                    "time": time.time()
                }
                state.data.setdefault("events", []).append({
                    "time": time.time(),
                    "message": f"CRITICAL: {symbol} base-asset commission check failed ({fee_err}); recovery locked.",
                })
                state.data["events"] = state.data["events"][-200:]
                state.save()
            notify(f"CRITICAL: {symbol} base fee check failed ({fee_err}); recovery lock enabled to prevent OCO failure.")
            return

        net_received_decimal = max(Decimal("0"), filled_decimal - base_fee)
        if base_fee > 0:
            log.info("%s: base-asset commission deducted: %s %s (gross=%s, net=%s)",
                     symbol, base_fee, base_asset, filled_decimal, net_received_decimal)
        # Keep the exact received quantity for accounting. The order quantity sent to
        # an OCO must obey LOT_SIZE, so track any difference as explicitly unprotected.
        filled_qty = format(net_received_decimal, "f")
        oco_qty = fmt_qty(self.client, symbol, net_received_decimal)
        fill_residual_decimal = net_received_decimal - Decimal(str(oco_qty))
        fill_residual_text = format(fill_residual_decimal, "f")
        # Ensure protective levels remain on correct sides of actual average fill.
        actual_stop = min(stop, avg * (1 - 0.001))
        realized_risk = avg - actual_stop
        if realized_risk <= 0 or (realized_risk / avg) < 0.005:
            log.warning("%s: realized stop distance (%.4f) unusually tight after fill avg=%.8g stop=%.8g", symbol, realized_risk, avg, actual_stop)
        # Compute target from actual average fill using EXIT_MODE ("FIXED_TP" or "2R")
        actual_target = compute_target_price(avg, actual_stop)
        target_s=fmt_price(self.client,symbol,actual_target,upward=True)
        stop_s=fmt_price(self.client,symbol,actual_stop)
        # Stop-limit is set 0.2% below stop trigger to improve chance of execution.
        stop_limit_s=fmt_price(self.client,symbol,float(stop_s)*0.998)
        # Persist the exact fill and a recovery lock BEFORE submitting OCO.
        provisional = Position(symbol,filled_qty,avg,float(stop_s),float(target_s),oid,None,time.time(),float(sig["level"]))
        provisional_data = asdict(provisional)
        provisional_data["oco_covered_quantity"] = oco_qty
        provisional_data["unprotected_quantity"] = fill_residual_text
        provisional_data["exit_mode"] = EXIT_MODE
        with state.lock:
            state.data["positions"][symbol] = provisional_data
            state.data["manual_pause"] = True
            state.data["recovery_required"] = {
                "symbol": symbol, "quantity": filled_qty, "entry_order_id": oid,
                "reason": "BUY filled; protective OCO not yet confirmed in persisted state; reconcile exchange before resuming",
                "time": time.time()
            }
            state.save()

        # OCO Maker Guard: Binance LIMIT_MAKER requires target > current market price for SELL.
        # Check current price so an already-breached target or stop doesn't fail into recovery lock.
        try:
            curr_px = float(self.client.price(symbol))
        except Exception:
            curr_px = avg

        if curr_px >= float(target_s):
            log.info("%s: price reached target (%.8g >= %s) immediately after BUY; closing at market directly without OCO", symbol, curr_px, target_s)
            return self._direct_market_close_no_oco(symbol, Decimal(str(oco_qty)), avg, oid, stop_s, target_s, "TARGET")
        if curr_px <= float(stop_s):
            log.warning("%s: price breached stop (%.8g <= %s) immediately after BUY; closing at market directly without OCO", symbol, curr_px, stop_s)
            return self._direct_market_close_no_oco(symbol, Decimal(str(oco_qty)), avg, oid, stop_s, target_s, "STOP")

        try:
            if Decimal(str(oco_qty)) <= 0:
                raise RuntimeError("Filled quantity is below the exchange LOT_SIZE step; no OCO quantity is available.")
            oco=self.client.oco_sell(symbol,oco_qty,target_s,stop_s,stop_limit_s)
        except Exception as e:
            # The exchange may have accepted the OCO while its response was lost.
            # Keep the durable provisional position and recovery lock; never market-sell blindly.
            notify(
                f"CRITICAL: {symbol} buy may be filled but OCO result is uncertain: {e}. "
                "New entries paused. Check Binance Spot balances and open orders immediately; "
                "do not submit another sell until existing orders are verified."
            )
            return
        oco_id = oco.get("orderListId") if isinstance(oco, dict) else None
        pos=Position(symbol,filled_qty,avg,float(stop_s),float(target_s),oid,oco_id,time.time(),float(sig["level"]))
        with state.lock:
            state.data["positions"][symbol]=asdict(pos)
            state.data["positions"][symbol]["oco_covered_quantity"] = oco_qty
            state.data["positions"][symbol]["unprotected_quantity"] = fill_residual_text
            state.data["positions"][symbol]["exit_mode"] = EXIT_MODE
            state.data["trades"].append({"time":time.time(),"symbol":symbol,"status":"OPEN","entry":avg,"stop":float(stop_s),"target":float(target_s),"exit_mode":EXIT_MODE,"qty":filled_qty,"order_id":oid,"oco_id":oco_id})
            # Persist the OCO ID while the recovery lock is still active.
            state.save()

        # Verify the exchange-side OCO and both child orders BEFORE unlocking entries.
        # Any uncertain/mismatched result fails closed and leaves recovery_required active.
        try:
            if oco_id is None:
                raise RuntimeError("OCO response has no orderListId")
            info = self.client.request(
                "GET", "/api/v3/orderList",
                {"orderListId": oco_id}, signed=True
            )
            if info.get("symbol") != symbol:
                raise RuntimeError("OCO verification: symbol mismatch")
            if (
                info.get("listStatusType") != "EXEC_STARTED"
                or info.get("listOrderStatus") != "EXECUTING"
            ):
                raise RuntimeError(
                    "OCO verification: list not executing "
                    f"({info.get('listStatusType')}/{info.get('listOrderStatus')})"
                )

            children = info.get("orders", [])
            child_ids = [
                int(item["orderId"]) for item in children
                if item.get("orderId") is not None
            ]
            if len(children) != 2 or len(set(child_ids)) != 2:
                raise RuntimeError("OCO verification: expected two distinct child orders")

            child_quantities = []
            for child_id in child_ids:
                detail = self.client.query_order(symbol, child_id)
                if detail.get("symbol") != symbol:
                    raise RuntimeError(f"OCO verification: child {child_id} symbol mismatch")
                if int(detail.get("orderListId", -1)) != int(oco_id):
                    raise RuntimeError(f"OCO verification: child {child_id} linked to wrong list")
                if detail.get("side") != "SELL":
                    raise RuntimeError(f"OCO verification: child {child_id} is not SELL")
                if detail.get("status") != "NEW":
                    raise RuntimeError(
                        f"OCO verification: child {child_id} status is {detail.get('status')}"
                    )
                if Decimal(str(detail.get("executedQty", "0"))) != 0:
                    raise RuntimeError(f"OCO verification: child {child_id} has executions")
                child_quantities.append(Decimal(str(detail.get("origQty", "0"))))

            if child_quantities[0] != child_quantities[1]:
                raise RuntimeError("OCO verification: child quantities differ")

            filled_decimal = Decimal(str(filled_qty))
            covered_decimal = child_quantities[0]
            if covered_decimal <= 0 or covered_decimal > filled_decimal:
                raise RuntimeError("OCO verification: covered quantity is invalid")

            covered_text = format(covered_decimal, "f")
            residual_decimal = filled_decimal - covered_decimal
            residual_text = format(residual_decimal, "f")
            with state.lock:
                state.data["positions"][symbol]["oco_covered_quantity"] = covered_text
                state.data["positions"][symbol]["unprotected_quantity"] = residual_text

                if residual_decimal > 0:
                    state.data["manual_pause"] = True
                    state.data["recovery_required"] = {
                        "symbol": symbol,
                        "quantity": filled_qty,
                        "entry_order_id": oid,
                        "oco_order_list_id": oco_id,
                        "reason": (
                            f"OCO verified but covers {covered_text} of {filled_qty}; "
                            f"uncovered residual is {residual_text}. Manual reconciliation required."
                        ),
                        "time": time.time(),
                    }
                    state.save()
                    log.critical(
                        "%s OCO covers %s of %s; residual %s. Entries remain locked.",
                        symbol, covered_text, filled_qty, residual_text
                    )
                    notify(
                        f"CRITICAL: {symbol} OCO covers only {covered_text} of {filled_qty}; "
                        f"{residual_text} remains uncovered. Trading stays paused."
                    )
                    return

            if covered_decimal != filled_decimal:
                raise RuntimeError("OCO verification: protected quantity does not equal filled quantity")

        except Exception as e:
            with state.lock:
                state.data["manual_pause"] = True
                state.data["recovery_required"] = {
                    "symbol": symbol,
                    "quantity": filled_qty,
                    "entry_order_id": oid,
                    "oco_order_list_id": oco_id,
                    "reason": f"OCO verification failed; reconcile exchange before resuming: {e}",
                    "time": time.time(),
                }
                state.save()
            log.critical("%s OCO verification failed; entries remain locked: %s", symbol, e)
            notify(
                f"CRITICAL: {symbol} OCO verification failed. Trading remains paused; "
                "check Binance order list, child orders, and balances."
            )
            return

        # Only clear the recovery lock after full exchange-side protection is verified.
        with state.lock:
            prev_recovery = state.data.get("recovery_required")
            state.data["recovery_required"] = None
            state.data["manual_pause"] = False
            saved_ok = False
            try:
                saved_ok = state.save()
            except Exception:
                saved_ok = False
            if not saved_ok:
                state.data["recovery_required"] = prev_recovery or {
                    "symbol": symbol, "quantity": str(filled_qty), "entry_order_id": oid,
                    "oco_order_list_id": oco_id,
                    "reason": "OCO verified but recovery unlock could not be persisted; reconcile before resuming",
                    "time": time.time()
                }
                state.data["manual_pause"] = True
                log.critical("Could not persist recovery unlock for %s; keeping trading locked.", symbol)
        notify(f"{symbol}: position protected by verified OCO ({EXIT_MODE}). qty={filled_qty}, avg={avg:.8g}, stop={stop_s}, target={target_s}")
        notify_trade_dashboard_snapshot("ENTRY FILLED AND OCO VERIFIED", symbol)

    def _handle_completed_oco(self, symbol, position, oco_id, info):
        """Automatically close a position only when exchange confirms a full OCO exit; otherwise fail closed."""
        if info.get("symbol") != symbol:
            raise RuntimeError("Completed OCO symbol mismatch")
        children = info.get("orders", [])
        child_ids = [int(x["orderId"]) for x in children if x.get("orderId") is not None]
        if len(children) != 2 or len(set(child_ids)) != 2:
            raise RuntimeError("Completed OCO does not contain two distinct child orders")
        details = []
        for child_id in child_ids:
            order = self.client.query_order(symbol, child_id)
            if order.get("symbol") != symbol or int(order.get("orderListId", -1)) != int(oco_id):
                raise RuntimeError(f"Completed OCO child {child_id} has a symbol/list mismatch")
            if order.get("side") != "SELL":
                raise RuntimeError(f"Completed OCO child {child_id} is not SELL")
            details.append(order)
        fills = [o for o in details if o.get("status") == "FILLED" and Decimal(str(o.get("executedQty", "0"))) > 0]
        siblings = [o for o in details if o.get("status") in ("CANCELED", "EXPIRED") and Decimal(str(o.get("executedQty", "0"))) == 0]
        if len(fills) != 1 or len(siblings) != 1:
            raise RuntimeError("Completed OCO is ambiguous: expected one fully filled SELL and one unexecuted cancelled sibling")
        filled_order = fills[0]
        if filled_order.get("type") in ("STOP_LOSS_LIMIT", "STOP_LOSS"):
            oco_status = "CLOSED_OCO_STOP"
        elif filled_order.get("type") in ("LIMIT_MAKER", "LIMIT"):
            oco_status = "CLOSED_OCO_TARGET"
        else:
            oco_status = "CLOSED_OCO_EXIT"

        executed = Decimal(str(filled_order.get("executedQty", "0")))
        protected = Decimal(str(position.get("oco_covered_quantity") or "0"))
        position_qty = Decimal(str(position.get("quantity") or "0"))
        residual = Decimal(str(position.get("unprotected_quantity") or "0"))
        quote = Decimal(str(filled_order.get("cummulativeQuoteQty", "0")))
        if residual != 0:
            raise RuntimeError(f"Completed OCO left a previously recorded uncovered residual of {residual}; manual reconciliation required")
        if protected <= 0 or protected != position_qty or executed != protected or quote <= 0:
            raise RuntimeError("Completed OCO does not prove a full-quantity exit; manual reconciliation required")
        entry_price = Decimal(str(position.get("entry_price", "0")))
        exit_price = quote / executed
        gross_pnl = quote - entry_price * executed
        entry_id = int(position.get("entry_order_id") or 0)
        trade = next((t for t in reversed(state.data.get("trades", []))
                      if t.get("status") == "OPEN" and t.get("symbol") == symbol
                      and int(t.get("order_id") or 0) == entry_id), None)
        if trade is None:
            raise RuntimeError("Completed OCO has no matching persisted OPEN trade; manual reconciliation required")
        trade.update({
            "status": oco_status,
            "exit": float(exit_price),
            "exit_time": time.time(),
            "exit_order_id": int(filled_order.get("orderId", 0)),
            "exit_quantity": format(executed, "f"),
            "gross_realized_pnl_usdt_estimate": float(gross_pnl),
            "pnl_note": f"Gross estimate before commissions; exchange OCO filled as {oco_status}.",
        })
        state.data["positions"].pop(symbol, None)
        rec_lock = state.data.get("recovery_required")
        if isinstance(rec_lock, dict) and rec_lock.get("symbol") == symbol:
            state.data["recovery_required"] = None
        if not state.data.get("positions"):
            self.set_flat_baseline()
        state.data.setdefault("events", []).append({
            "time": time.time(),
            "message": (
                f"{symbol} OCO fully exited ({oco_status}) {format(executed, 'f')} at ~{float(exit_price):.8g}; "
                f"gross PnL estimate {float(gross_pnl):+.4f} USDT before fees."
            ),
        })
        state.data["events"] = state.data["events"][-200:]
        state.save()
        notify_trade_dashboard_snapshot("OCO EXIT FILLED", symbol)
        log.info(
            "%s OCO exit verified: qty=%s exit~%.8g gross_pnl_estimate=%+.4f USDT before fees.",
            symbol, format(executed, "f"), float(exit_price), float(gross_pnl),
        )

    def _settle_paper_position(self, symbol: str, status: str, exit_price: float, exit_reason: str = "") -> dict | None:
        """Atomically settle a single paper position.
        Ensures idempotency, consistent trade ledger recording, and rebaselines paper cash
        without calling live balances() or set_flat_baseline()."""
        exit_px = float(exit_price)
        with state.lock:
            if symbol not in state.data.get("positions", {}):
                return None
            pos = state.data["positions"][symbol]
            trade_id = pos.get("trade_id")
            entry = float(pos.get("entry_price", 0.0))
            qty = float(pos.get("quantity", 0.0))

            rec = None
            if trade_id:
                rec = next((t for t in reversed(state.data.get("trades", [])) if t.get("id") == trade_id and t.get("status") == "OPEN"), None)
            if rec is None:
                rec = next((t for t in reversed(state.data.get("trades", [])) if t.get("symbol") == symbol and t.get("status") == "OPEN"), None)

            if rec is None:
                log.warning("%s: paper settlement could not find matching OPEN trade record (trade_id=%s)", symbol, trade_id)
                return None

            state.data["positions"].pop(symbol, None)
            net_pnl = qty * (exit_px * (1.0 - PAPER_FEE) - entry * (1.0 + PAPER_FEE))
            now = time.time()

            rec.update({
                "status": status,
                "exit": round(exit_px, 8),
                "exit_time": now,
                "exit_reason": exit_reason or status,
                "pnl": round(net_pnl, 4),
                "gross_realized_pnl_usdt_estimate": round(net_pnl, 4),
                "is_paper": True,
            })

            # Rebaseline flat paper cash equity without live balance calls
            if not state.data.get("positions"):
                initial_capital = 1000.0
                paper_realized = 0.0
                for t in state.data.get("trades", []):
                    if not isinstance(t, dict):
                        continue
                    if t.get("status") in TERMINAL_TRADE_STATUSES:
                        pnl_val = t.get("pnl")
                        if pnl_val is None:
                            pnl_val = t.get("gross_realized_pnl_usdt_estimate", 0.0)
                        try:
                            v = float(pnl_val)
                            if math.isfinite(v):
                                paper_realized += v
                        except (ValueError, TypeError):
                            pass

                try:
                    flows_val = float(state.data.get("net_external_flows_usdt", 0.0) or 0.0)
                    net_flows = flows_val if math.isfinite(flows_val) else 0.0
                except (ValueError, TypeError):
                    net_flows = 0.0

                paper_cash = initial_capital + paper_realized + net_flows
                baseline = round(paper_cash - net_flows, 4)
                if not math.isfinite(baseline) or baseline <= 0:
                    baseline = round(initial_capital, 4)
                state.data["last_account_equity_usdt"] = baseline

            state.save()

        label = status
        if status == "CLOSED_TARGET":
            label = "TARGET HIT"
        elif status == "CLOSED_STOP":
            label = "STOP HIT"
        elif status == "CLOSED_TIME_STOP":
            label = "TIME STOP HIT"
        elif status == "CLOSED_MANUAL_MARKET":
            label = "MANUAL MARKET CLOSE"
        notify(f"{symbol} PAPER {label}: {net_pnl:+.4f} USDT (fees and slippage included)")
        return rec

    def close_position_at_market(self, symbol, is_time_stop=False, is_stop_fallback=False):
        """Cancel and verify OCO first, then market-sell the recorded position once.
        In paper mode, closes position immediately at fresh price."""
        symbol = str(symbol).upper()
        rec_lock = state.data.get("recovery_required")
        if rec_lock:
            if not isinstance(rec_lock, dict) or rec_lock.get("symbol") is None:
                raise RuntimeError("Cannot close while account-level recovery lock is active; reconcile exchange state first.")

        pos = state.data.get("positions", {}).get(symbol)
        if not isinstance(pos, dict):
            raise RuntimeError(f"No tracked open position exists for {symbol}.")

        if not LIVE or pos.get("is_paper"):
            try:
                fresh_px = float(self.client.price(symbol))
            except Exception:
                fresh_px = float(pos.get("entry_price", 0.0))
            exit_px = fresh_px * (1 - (STOP_SLIPPAGE if is_stop_fallback else PAPER_SLIPPAGE))
            status_str = "CLOSED_STOP_FALLBACK" if is_stop_fallback else ("CLOSED_TIME_STOP" if is_time_stop else "CLOSED_MANUAL_MARKET")
            rec = self._settle_paper_position(symbol, status_str, exit_px, exit_reason=status_str)
            if not rec:
                raise RuntimeError(f"Failed to settle paper position for {symbol}.")
            return {"ok": True, "symbol": symbol, "exit_price": exit_px, "pnl": rec.get("pnl", 0.0)}
        oco_id = pos.get("oco_order_list_id")
        if oco_id is None:
            raise RuntimeError("Position has no recorded OCO order-list ID; refusing unprotected market close.")
        qty_recorded = Decimal(str(pos.get("quantity") or "0"))
        covered = Decimal(str(pos.get("oco_covered_quantity") or pos.get("quantity") or "0"))
        residual = Decimal(str(pos.get("unprotected_quantity") or "0"))
        if qty_recorded <= 0 or covered != qty_recorded or residual != 0:
            raise RuntimeError("Position quantity is not fully covered by its verified OCO; manual reconciliation required.")

        # Pre-check: Verify OCO exists and is in executing state before cancellation
        info = self.client.request("GET", "/api/v3/orderList", {"orderListId": oco_id}, signed=True)
        if info.get("symbol") != symbol:
            raise RuntimeError(f"OCO symbol mismatch: expected {symbol}, got {info.get('symbol')}")
        if info.get("listOrderStatus") == "ALL_DONE":
            log.info("%s: OCO %s already completed on exchange; resolving OCO fill.", symbol, oco_id)
            self._handle_completed_oco(symbol, pos, oco_id, info)
            return {"ok": True, "symbol": symbol, "status": "CLOSED_OCO_RESOLVED"}
        if info.get("listStatusType") != "EXEC_STARTED" or info.get("listOrderStatus") != "EXECUTING":
            raise RuntimeError("OCO is not in verified active state; refresh dashboard and reconcile before retrying.")
        children = info.get("orders", [])
        if len(children) != 2 or len({int(x.get("orderId", -1)) for x in children}) != 2:
            raise RuntimeError("OCO child orders are incomplete; refusing market close.")

        qty_step = self.client.filters.get(symbol, {}).get("LOT_SIZE", {}).get("stepSize")
        if not qty_step:
            raise RuntimeError("Exchange quantity step size is unavailable; refusing market sell.")
        sell_qty = Decimal(floor_step(qty_recorded, qty_step))
        if sell_qty <= 0 or sell_qty != qty_recorded:
            raise RuntimeError(f"Step size calculation resulted in invalid sell quantity ({sell_qty} != {qty_recorded})")

        # --- STEP 1: CANCEL OCO ---
        proceed_to_step3 = False
        try:
            cancel_res = self.client.request("DELETE", "/api/v3/orderList", {"orderListId": oco_id}, signed=True)
        except Exception as cancel_exc:
            # Re-query GET /api/v3/orderList before setting recovery_required.
            # If the OCO already filled on exchange (e.g. cancel raced with fill or timed out), resolve cleanly!
            log.warning("%s: DELETE orderList %s raised exception (%s); re-querying OCO status...", symbol, oco_id, cancel_exc)
            try:
                fresh_info = self.client.request("GET", "/api/v3/orderList", {"orderListId": oco_id}, signed=True)
                if fresh_info.get("listOrderStatus") == "ALL_DONE":
                    child_orders = fresh_info.get("orderReports", []) or fresh_info.get("orders", [])
                    has_filled_leg = False
                    leg_query_failed = False
                    for ord_item in child_orders:
                        ord_status = ord_item.get("status")
                        ord_exec = Decimal(str(ord_item.get("executedQty", "0")))
                        if ord_status == "FILLED" and ord_exec > 0:
                            has_filled_leg = True
                            break
                        if ord_status is None and ord_item.get("orderId"):
                            try:
                                full_ord = self.client.request("GET", "/api/v3/order", {"symbol": symbol, "orderId": ord_item["orderId"]}, signed=True)
                                if full_ord.get("status") == "FILLED" and Decimal(str(full_ord.get("executedQty", "0"))) > 0:
                                    has_filled_leg = True
                                    break
                            except Exception as leg_exc:
                                log.error("%s: Failed to query OCO child order %s after cancel timeout: %s", symbol, ord_item["orderId"], leg_exc)
                                leg_query_failed = True
                                cancel_exc = RuntimeError(f"Failed to query OCO child order {ord_item['orderId']}: {leg_exc}")
                                break
                    if leg_query_failed:
                        # Cannot confirm if child order filled; fail-closed to recovery_required lock without placing market sell
                        proceed_to_step3 = False
                    elif has_filled_leg:
                        log.info("%s: Re-queried OCO %s status is ALL_DONE with filled child leg; resolving completed OCO fill.", symbol, oco_id)
                        self._handle_completed_oco(symbol, pos, oco_id, fresh_info)
                        return {"ok": True, "symbol": symbol, "status": "CLOSED_OCO_RESOLVED_AFTER_CANCEL_TIMEOUT"}
                    else:
                        log.info("%s: Re-queried OCO %s is ALL_DONE but no child leg filled (legs canceled/expired). Proceeding to market sell.", symbol, oco_id)
                        cancel_res = fresh_info
                        proceed_to_step3 = True
            except Exception as query_exc:
                log.error("%s: Failed to re-query OCO %s after cancel exception: %s", symbol, oco_id, query_exc)

            if not proceed_to_step3:
                with state.lock:
                    state.data["manual_pause"] = True
                    state.data["recovery_required"] = {
                        "symbol": symbol,
                        "quantity": str(qty_recorded),
                        "oco_order_list_id": oco_id,
                        "reason": f"Failed to cancel OCO order list {oco_id}: {cancel_exc}; OCO may still be active. Reconcile exchange before resuming.",
                        "time": time.time(),
                    }
                    state.save()
                log.critical("%s: Failed to cancel OCO (%s); entries locked.", symbol, cancel_exc)
                notify(f"CRITICAL: {symbol} failed to cancel OCO ({cancel_exc}). Trading locked.")
                raise RuntimeError(f"Failed to cancel OCO ({cancel_exc}); recovery locked.") from cancel_exc

        # --- STEP 2: CONFIRM OCO CANCELED AND NEITHER LEG FILLED ---
        order_reports = cancel_res.get("orderReports", [])
        filled_reports = [r for r in order_reports if r.get("status") == "FILLED" and Decimal(str(r.get("executedQty", "0"))) > 0]
        if filled_reports:
            log.info("%s: OCO child leg filled before cancel took effect; resolving completed OCO exit.", symbol)
            oco_info = cancel_res if cancel_res.get("orders") else {"symbol": symbol, "orders": children, "orderReports": order_reports}
            self._handle_completed_oco(symbol, pos, oco_id, oco_info)
            return {"ok": True, "symbol": symbol, "status": "CLOSED_OCO_FILLED_BEFORE_CANCEL"}

        partially_filled = [r for r in order_reports if r.get("status") == "PARTIALLY_FILLED"]
        if partially_filled:
            with state.lock:
                state.data["manual_pause"] = True
                state.data["recovery_required"] = {
                    "symbol": symbol,
                    "quantity": str(qty_recorded),
                    "oco_order_list_id": oco_id,
                    "reason": "OCO cancellation returned partially filled child orders; manual reconciliation required.",
                    "time": time.time(),
                }
                state.save()
            log.critical("%s: OCO child partially filled during cancellation; recovery locked.", symbol)
            notify(f"CRITICAL: {symbol} OCO partially filled during cancel. Recovery locked.")
            raise RuntimeError("OCO child partially filled during cancel; recovery locked.")

        # --- STEP 3: RE-CHECK FREE BALANCE ON EXCHANGE ---
        base_asset = symbol[:-4] if symbol.endswith("USDT") else symbol
        try:
            acct = self.client.account()
            bal = next((b for b in acct.get("balances", []) if b.get("asset") == base_asset), None)
            free_bal = Decimal(str(bal.get("free", "0"))) if bal else Decimal("0")
        except Exception as acct_exc:
            with state.lock:
                state.data["manual_pause"] = True
                state.data["recovery_required"] = {
                    "symbol": symbol,
                    "quantity": str(qty_recorded),
                    "oco_order_list_id": oco_id,
                    "reason": f"OCO canceled but exchange balance query failed ({acct_exc}); coins unlocked in wallet. Reconcile exchange before resuming.",
                    "time": time.time(),
                }
                state.save()
            log.critical("%s: Balance query failed after OCO cancel (%s); recovery locked.", symbol, acct_exc)
            notify(f"CRITICAL: {symbol} balance query failed after OCO cancel ({acct_exc}). Coins unlocked; trading locked.")
            raise RuntimeError(f"Balance check failed after OCO cancel: {acct_exc}") from acct_exc

        if free_bal < sell_qty:
            with state.lock:
                state.data["manual_pause"] = True
                state.data["recovery_required"] = {
                    "symbol": symbol,
                    "quantity": str(qty_recorded),
                    "oco_order_list_id": oco_id,
                    "reason": f"OCO canceled but verified free balance ({free_bal} {base_asset}) is less than expected sell quantity ({sell_qty}). Recovery locked.",
                    "time": time.time(),
                }
                state.save()
            log.critical("%s: Free balance %s < sell qty %s after OCO cancel; recovery locked.", symbol, free_bal, sell_qty)
            notify(f"CRITICAL: {symbol} free balance insufficient after OCO cancel. Recovery locked.")
            raise RuntimeError(f"Free balance ({free_bal}) < sell quantity ({sell_qty}) after OCO cancel.")

        # --- STEP 4: SUBMIT MARKET SELL ---
        try:
            order = self.client.order({
                "symbol": symbol,
                "side": "SELL",
                "type": "MARKET",
                "quantity": format(sell_qty, "f"),
                "newOrderRespType": "FULL"
            })
        except Exception as sell_exc:
            # Unprotected Window Guard: OCO canceled, but market sell failed.
            # Attempt to re-place emergency stop protection before raising.
            log.critical("%s: Market sell failed after OCO cancel (%s); attempting emergency stop protection.", symbol, sell_exc)
            emergency_protection_placed = False
            emergency_order_id = None
            stop_px = float(pos.get("stop_price", 0))
            if stop_px > 0:
                try:
                    px_filter = self.client.filters.get(symbol, {}).get("PRICE_FILTER", {})
                    tick_size = px_filter.get("tickSize", "0.01")
                    limit_stop_px = floor_step(Decimal(str(stop_px)) * Decimal("0.995"), tick_size)
                    em_order = self.client.order({
                        "symbol": symbol,
                        "side": "SELL",
                        "type": "STOP_LOSS_LIMIT",
                        "quantity": format(sell_qty, "f"),
                        "stopPrice": format(Decimal(str(stop_px)), "f"),
                        "price": format(Decimal(str(limit_stop_px)), "f"),
                        "timeInForce": "GTC",
                    })
                    emergency_order_id = em_order.get("orderId")
                    emergency_protection_placed = True
                    log.warning("%s: Emergency stop protection placed on Binance (orderId=%s)", symbol, emergency_order_id)
                except Exception as em_err:
                    log.critical("%s: Emergency stop re-placement failed (%s); POSITION IS UNPROTECTED IN WALLET!", symbol, em_err)

            with state.lock:
                state.data["manual_pause"] = True
                state.data["recovery_required"] = {
                    "symbol": symbol,
                    "quantity": str(qty_recorded),
                    "oco_order_list_id": oco_id,
                    "emergency_protection_placed": emergency_protection_placed,
                    "emergency_order_id": emergency_order_id,
                    "unprotected_window": not emergency_protection_placed,
                    "reason": (
                        f"Market sell failed after OCO cancel ({sell_exc}). "
                        f"{'Emergency stop order placed (orderId=' + str(emergency_order_id) + '); ' if emergency_protection_placed else 'CRITICAL: Position has NO stop and is UNPROTECTED in spot wallet (coins unlocked in wallet)! '} "
                        "Reconcile exchange immediately."
                    ),
                    "time": time.time(),
                }
                state.save()

            if emergency_protection_placed:
                notify(f"CRITICAL: {symbol} market sell failed after OCO cancel; emergency stop placed ({emergency_order_id}). Trading locked.")
            else:
                notify(f"CRITICAL EMERGENCY: UNPROTECTED WINDOW on {symbol}! OCO cancel succeeded but market sell failed ({sell_exc}). {qty_recorded} is UNPROTECTED in spot wallet! MANUAL INTERVENTION REQUIRED IMMEDIATELY!")
            raise RuntimeError(f"Market sell failed after OCO cancel ({sell_exc}); {'emergency stop placed' if emergency_protection_placed else 'position UNPROTECTED in wallet'}. Recovery locked.") from sell_exc

        # --- STEP 5: VERIFY FULL FILL ---
        executed = Decimal(str(order.get("executedQty", "0")))
        quote = Decimal(str(order.get("cummulativeQuoteQty", "0")))
        if order.get("status") != "FILLED" or executed != sell_qty or quote <= 0:
            with state.lock:
                state.data["manual_pause"] = True
                state.data["recovery_required"] = {
                    "symbol": symbol,
                    "quantity": str(qty_recorded),
                    "oco_order_list_id": oco_id,
                    "reason": f"Manual market SELL result is not a verified full fill (status={order.get('status')}, executed={executed}); inspect Binance order/trades and reconcile.",
                    "time": time.time(),
                }
                state.save()
            log.critical("%s: Market sell not a verified full fill (status=%s, executed=%s); recovery locked.", symbol, order.get("status"), executed)
            notify(f"CRITICAL: {symbol} manual market sell did not verify as a full fill. Trading locked; inspect Binance order {order.get('orderId')} before retrying.")
            raise RuntimeError("Market sell is not a verified full fill. Trading is locked; inspect Binance before retrying.")

        avg_exit = quote / executed
        entry = Decimal(str(pos.get("entry_price", "0")))
        gross_pnl = quote - entry * executed
        entry_id = int(pos.get("entry_order_id") or 0)
        status_label = "CLOSED_STOP_FALLBACK" if is_stop_fallback else ("CLOSED_TIME_STOP" if is_time_stop else "CLOSED_MANUAL_MARKET")
        with state.lock:
            trade = next((t for t in reversed(state.data.get("trades", [])) if t.get("status") == "OPEN" and t.get("symbol") == symbol and int(t.get("order_id") or 0) == entry_id), None)
            if trade is None:
                state.data["manual_pause"] = True
                state.data["recovery_required"] = {"symbol": symbol, "quantity": str(qty_recorded), "reason": "Market sell filled but matching OPEN trade record could not be found; reconcile trade history manually.", "time": time.time()}
                state.save()
                notify(f"CRITICAL: {symbol} market sell filled but local trade record is inconsistent. Reconcile Binance and local state.")
                raise RuntimeError("Market sell filled but local trade record is inconsistent; recovery lock enabled.")
            trade.update({"status": status_label, "exit": float(avg_exit), "exit_time": time.time(), "exit_order_id": int(order.get("orderId", 0)), "exit_quantity": format(executed, "f"), "gross_realized_pnl_usdt_estimate": float(gross_pnl), "pnl_note": f"Manual market exit ({status_label}); gross estimate before commissions."})
            state.data["positions"].pop(symbol, None)
            # Item 3: Success clears ONLY its own lock
            rec_lock = state.data.get("recovery_required")
            if isinstance(rec_lock, dict) and rec_lock.get("symbol") == symbol:
                state.data["recovery_required"] = None
                if state.data.get("manual_pause_reason") in ("dashboard_manual_close_in_progress", "dashboard_manual_close_uncertain"):
                    state.data["manual_pause"] = False
                    state.data["manual_pause_reason"] = None
            state.save()
        if not state.data.get("positions"):
            self.set_flat_baseline()
        msg = f"{status_label} FILLED: {symbol} qty={executed}, avg exit={float(avg_exit):.8g}, gross PnL estimate={float(gross_pnl):+.4f} USDT before fees/slippage, order={order.get('orderId')}"
        notify(msg)
        notify_trade_dashboard_snapshot(f"{status_label} FILLED", symbol)
        return {"ok": True, "symbol": symbol, "quantity": format(executed, "f"), "average_exit": float(avg_exit), "gross_pnl_usdt_estimate": float(gross_pnl), "order_id": order.get("orderId")}

    def _paper_check(self, symbol, p):
        """Settle one paper position from CLOSED 15m candles (wicks included), with the same rules
        as backtest.py: the candle in which the position opened is skipped, the stop is assumed to
        hit before the target inside one candle, time-stop is enforced (when MAX_HOLD_HOURS > 0),
        and exits include slippage and fees."""
        with state.lock:
            # Idempotency check: if position was already popped or replaced, abort
            if symbol not in state.data.get("positions", {}):
                return
            current_pos = state.data["positions"][symbol]
            if p.get("trade_id") and current_pos.get("trade_id") != p.get("trade_id"):
                return

        stop, target = float(p["stop_price"]), float(p["target_price"])
        raw_opened = float(p.get("opened_at") or time.time())
        opened_ms = int(raw_opened if raw_opened > 1e11 else raw_opened * 1000)

        # Calculate the close time of the candle in which the trade opened
        open_candle_close = int(p.get("open_candle_close_time") or 0)
        if open_candle_close == 0:
            open_candle_close = (opened_ms // (15 * 60 * 1000) + 1) * (15 * 60 * 1000) - 1
            p["open_candle_close_time"] = open_candle_close

        checked = int(p.get("paper_checked_close_time") or 0)
        if checked < open_candle_close:
            checked = open_candle_close
            p["paper_checked_close_time"] = checked

        status = exit_px = exit_reason = None
        last_ct = checked
        start_ms = checked + 1

        try:
            klines = self.client.klines(symbol, "15m", 100, start_time=start_ms)
        except Exception as e:
            log.warning("%s: failed to fetch 15m candles for paper check: %s", symbol, e)
            return

        for r in closed_rows(klines):
            ct = int(r[6])
            # The candle where the trade opened is excluded from the check
            if ct <= open_candle_close:
                continue
            if ct <= checked:
                continue
            last_ct = ct
            # Stop check first within candle
            if float(r[3]) <= stop:
                status, exit_px, exit_reason = "CLOSED_STOP", min(stop, float(r[1])) * (1 - STOP_SLIPPAGE), "STOP_HIT"
                break
            # Target check
            if float(r[2]) >= target:
                status, exit_px, exit_reason = "CLOSED_TARGET", target, "TARGET_HIT"
                break
            # Time-stop check: ONLY trigger when MAX_HOLD_HOURS > 0 and closed 15m candle close time >= opened_at + MAX_HOLD_HOURS
            if MAX_HOLD_HOURS > 0 and ct >= opened_ms + MAX_HOLD_HOURS * 3600 * 1000:
                status, exit_px, exit_reason = "CLOSED_TIME_STOP", float(r[4]) * (1 - PAPER_SLIPPAGE), "TIME_STOP_HIT"
                break

        if status is None:
            if last_ct != checked:
                with state.lock:
                    if symbol in state.data.get("positions", {}):
                        state.data["positions"][symbol]["paper_checked_close_time"] = last_ct
                        state.save()
            return

        self._settle_paper_position(symbol, status, exit_px, exit_reason=exit_reason)

    def settle_paper_positions(self):
        """Called from run() outside live mode: startup_preflight() returns early there, so
        reconcile() (which used to hold the paper exit code) never ran."""
        for symbol, p in list(state.data["positions"].items()):
            if not isinstance(p, dict) or not (p.get("is_paper") or not LIVE):
                continue
            try:
                self._paper_check(symbol, p)
            except Exception as e:
                log.warning("Paper position evaluation for %s failed: %s", symbol, e)

    def enforce_time_stop(self):
        """Live trading time stop: exit at market after MAX_HOLD_HOURS (disabled if MAX_HOLD_HOURS <= 0)."""
        if not LIVE or MAX_HOLD_HOURS <= 0:
            return
        now = time.time()
        for symbol, p in list(state.data.get("positions", {}).items()):
            if isinstance(p, dict) and not p.get("is_paper"):
                opened_at = float(p.get("opened_at") or 0)
                if opened_at > 0 and (now - opened_at) >= MAX_HOLD_HOURS * 3600:
                    log.info("%s: %dh max hold time reached (opened %.1f hours ago); executing market time-stop close",
                             symbol, MAX_HOLD_HOURS, (now - opened_at) / 3600)
                    try:
                        self.close_position_at_market(symbol, is_time_stop=True)
                    except Exception as exc:
                        log.critical("%s: time-stop market close failed: %s", symbol, exc)
                        notify(f"CRITICAL: {symbol} time-stop close failed: {exc}")

    def reconcile(self):
        """Live exchange OCO verification and reconciliation."""
        if not LIVE:
            return
        # Verify the OCO and its child orders. Never auto-unlock a recovered position.
        for symbol, p in list(state.data["positions"].items()):
            if p.get("is_paper"):
                continue
            try:
                oco_id = p.get("oco_order_list_id")
                if oco_id is None:
                    raise RuntimeError("Position has no OCO ID")

                info = self.client.request(
                    "GET", "/api/v3/orderList", {"orderListId": oco_id}, signed=True
                )
                if info.get("symbol") != symbol:
                    raise RuntimeError(
                        f"OCO symbol mismatch: expected {symbol}, got {info.get('symbol')}"
                    )
                if (
                    info.get("listStatusType") == "ALL_DONE"
                    and info.get("listOrderStatus") == "ALL_DONE"
                ):
                    self._handle_completed_oco(symbol, p, oco_id, info)
                    continue

                # Watchdog price check against unfilled STOP_LOSS_LIMIT order:
                # Only runs if OCO is NOT completed (ALL_DONE) on exchange
                try:
                    curr_px = float(self.client.price(symbol))
                    stop_px = float(p.get("stop_price", 0))
                    if stop_px > 0 and curr_px < stop_px * 0.995:
                        log.critical("%s: Market price (%.8g) fell significantly below stop price (%.8g) without full OCO fill; executing watchdog market sell.", symbol, curr_px, stop_px)
                        notify(f"WATCHDOG TRIGGERED: {symbol} market price fell below stop without fill; closing at market.")
                        self.close_position_at_market(symbol)
                        continue
                except Exception as px_err:
                    log.warning("%s: Watchdog price check error (%s); continuing reconciliation.", symbol, px_err)

                if (
                    info.get("listStatusType") != "EXEC_STARTED"
                    or info.get("listOrderStatus") != "EXECUTING"
                ):
                    raise RuntimeError(
                        "OCO is neither actively executing nor a verified completed exit: "
                        f"{info.get('listStatusType')}/{info.get('listOrderStatus')}"
                    )

                children = info.get("orders", [])
                child_ids = {
                    int(item["orderId"])
                    for item in children
                    if item.get("orderId") is not None
                }
                if len(children) != 2 or len(child_ids) != 2:
                    raise RuntimeError("OCO does not contain exactly two distinct child orders")

                # Use the specifically protected quantity, not the full BUY fill.
                expected_qty = float(
                    p.get("oco_covered_quantity") or p.get("quantity") or 0
                )
                if expected_qty <= 0:
                    raise RuntimeError("Expected OCO-covered quantity is missing or invalid")

                child_details = []
                for child in children:
                    child_id = int(child["orderId"])
                    detail = self.client.query_order(symbol, child_id)
                    if detail.get("symbol") != symbol:
                        raise RuntimeError(f"Child {child_id} symbol mismatch")
                    if int(detail.get("orderListId", -1)) != int(oco_id):
                        raise RuntimeError(f"Child {child_id} is not linked to OCO {oco_id}")
                    if detail.get("side") != "SELL":
                        raise RuntimeError(f"Child {child_id} is not a SELL order")
                    child_details.append(detail)

                # Fix 9: Stop-Loss Triggered Unfilled Monitor
                # If the stop leg has triggered (take-profit leg canceled/expired, or curr_px <= stop)
                # and the limit order remains unfilled for >= 30 seconds, execute fallback market sell.
                stop_child = next((c for c in child_details if c.get("type") in ("STOP_LOSS_LIMIT", "STOP_LOSS")), None)
                tp_child = next((c for c in child_details if c.get("type") == "LIMIT_MAKER"), None)

                curr_px = float(self.client.price(symbol)) if hasattr(self.client, "price") else 0.0
                stop_px = float(p.get("stop_price", 0))

                is_stop_triggered = False
                if tp_child and tp_child.get("status") in ("CANCELED", "EXPIRED"):
                    is_stop_triggered = True
                elif stop_px > 0 and curr_px > 0 and curr_px <= stop_px:
                    is_stop_triggered = True

                if is_stop_triggered and stop_child and float(stop_child.get("executedQty", "0")) < expected_qty:
                    triggered_at = p.get("stop_triggered_at")
                    now = time.time()
                    if not triggered_at:
                        p["stop_triggered_at"] = now
                        with state.lock:
                            if symbol in state.data.get("positions", {}):
                                state.data["positions"][symbol]["stop_triggered_at"] = now
                                state.save()
                        log.warning("%s: STOP_LOSS_LIMIT triggered on exchange; monitoring fill (started at %.1f)", symbol, now)
                    elif now - float(triggered_at) >= 30:
                        log.critical("%s: STOP_LOSS_LIMIT triggered but unfilled after %.1fs (>=30s); executing fallback market exit.", symbol, now - float(triggered_at))
                        notify(f"FALLBACK TRIGGERED: {symbol} stop triggered but unfilled for >=30s; exiting at market.")
                        self.close_position_at_market(symbol, is_stop_fallback=True)
                        continue
                    else:
                        log.info("%s: STOP_LOSS_LIMIT triggered; waiting for fill (elapsed: %.1fs/30s)", symbol, now - float(triggered_at))
                        continue

                # Standard un-triggered state check: both child orders must be NEW and unfilled
                if not is_stop_triggered:
                    for detail in child_details:
                        cid = detail.get("orderId")
                        if detail.get("status") != "NEW":
                            raise RuntimeError(
                                f"Child {cid} status is {detail.get('status')}; manual reconciliation required"
                            )
                        actual_qty = float(detail.get("origQty", "0"))
                        if abs(actual_qty - expected_qty) > 1e-12:
                            raise RuntimeError(
                                f"Child {cid} quantity {actual_qty} does not match OCO-covered quantity {expected_qty}"
                            )
                        if float(detail.get("executedQty", "0")) != 0:
                            raise RuntimeError(
                                f"Child {cid} has executions; inspect trade history manually"
                            )

                # A known uncovered residual must keep the recovery lock active.
                residual = float(p.get("unprotected_quantity") or 0)
                if residual > 0:
                    state.data["manual_pause"] = True
                    if not state.data.get("recovery_required"):
                        state.data["recovery_required"] = {
                            "symbol": symbol,
                            "quantity": p.get("quantity"),
                            "entry_order_id": p.get("entry_order_id"),
                            "oco_order_list_id": oco_id,
                            "reason": (
                                f"OCO child orders verified, but {residual:.8f} BTC "
                                "remains uncovered. Manual reconciliation is required."
                            ),
                            "time": time.time(),
                        }
                    state.save()
                    log.warning(
                        "%s OCO child orders verified; uncovered residual=%s BTC. "
                        "Recovery lock remains active.",
                        symbol, p.get("unprotected_quantity")
                    )

            except Exception as e:
                state.data["manual_pause"] = True
                # Preserve existing recovery details, including residual information.
                recovery = state.data.get("recovery_required")
                if not isinstance(recovery, dict):
                    recovery = {}
                recovery.update({
                    "symbol": symbol,
                    "quantity": p.get("quantity"),
                    "entry_order_id": p.get("entry_order_id"),
                    "oco_order_list_id": p.get("oco_order_list_id"),
                    "reason": (
                        f"Reconciliation failed: {e}. Verify exchange state "
                        "before resuming trading."
                    ),
                    "time": time.time(),
                })
                state.data["recovery_required"] = recovery
                state.save()
                log.critical("Reconcile failed for %s; trading locked: %s", symbol, e)
                notify(
                    f"CRITICAL: {symbol} reconciliation failed. "
                    "Trading remains paused until exchange state is verified."
                )

    def startup_preflight(self):
        """Fail closed if existing positions or account-wide open orders are not fully tracked."""
        try:
            self.btc_regime()
        except Exception as e:
            log.warning("Initial BTC regime check warning: %s", e)
        if not LIVE or not API_KEY:
            log.info("Signal-only / demo preflight: running simulated ledger (no live exchange reconciliation needed).")
            return
        if state.data.get("recovery_required"):
            raise RuntimeError("Startup preflight refused: unresolved recovery state exists.")
        self.reconcile()
        if state.data.get("recovery_required"):
            raise RuntimeError("Startup preflight refused: position reconciliation failed or needs manual review.")

        expected_child_ids = set()
        for symbol, position in state.data.get("positions", {}).items():
            oco_id = position.get("oco_order_list_id")
            if oco_id is None:
                raise RuntimeError(f"Startup preflight refused: {symbol} position has no OCO ID.")
            info = self.client.request("GET", "/api/v3/orderList", {"orderListId": oco_id}, signed=True)
            if (
                info.get("symbol") != symbol
                or info.get("listStatusType") != "EXEC_STARTED"
                or info.get("listOrderStatus") != "EXECUTING"
            ):
                raise RuntimeError(f"Startup preflight refused: {symbol} OCO is not verified active.")
            children = info.get("orders", [])
            ids = {int(item["orderId"]) for item in children if item.get("orderId") is not None}
            if len(children) != 2 or len(ids) != 2:
                raise RuntimeError(f"Startup preflight refused: {symbol} OCO child list is incomplete.")
            expected_child_ids.update(ids)

        open_orders = self.client.request("GET", "/api/v3/openOrders", signed=True)
        try:
            actual_open_ids = {int(order["orderId"]) for order in open_orders}
        except Exception as exc:
            raise RuntimeError("Startup preflight refused: open-order response was malformed.") from exc

        if actual_open_ids != expected_child_ids:
            unexpected = sorted(actual_open_ids - expected_child_ids)
            missing = sorted(expected_child_ids - actual_open_ids)
            symbol = next(iter(state.data.get("positions", {})), None)
            state.data["manual_pause"] = True
            state.data["recovery_required"] = {
                "symbol": symbol,
                "quantity": state.data.get("positions", {}).get(symbol, {}).get("quantity") if symbol else None,
                "reason": (
                    "Startup found exchange open orders that do not exactly match the "
                    f"persisted OCO children; unexpected={unexpected}, missing={missing}. "
                    "Manual reconciliation required."
                ),
                "time": time.time(),
            }
            state.save()
            raise RuntimeError("Startup preflight refused: exchange open orders do not match saved state.")

        balances = self.balances()
        tracked_base_assets = {symbol[:-4] for symbol in state.data.get("positions", {}) if symbol.endswith("USDT")}
        allowed_fee_assets = {
            symbol[:-4] for symbol in EXCLUDED_SYMBOLS if symbol.endswith("USDT")
        }
        untracked_assets = {}
        for asset, values in balances.items():
            if asset == "USDT" or asset in tracked_base_assets or asset in allowed_fee_assets:
                continue
            amt = sum(values)
            if amt <= 0:
                continue
            sym = f"{asset}USDT"
            approx_val = 0.0
            if sym in self.client.latest_prices:
                approx_val = amt * float(self.client.latest_prices[sym])
            elif sym in self.client.filters:
                try:
                    approx_val = amt * float(self.client.price(sym))
                except Exception:
                    approx_val = 0.0
            # If greater than $1.00, it is a genuine untracked asset, not fee dust
            if approx_val > 1.0 or (sym not in self.client.filters and amt > 1.0):
                untracked_assets[asset] = values

        if untracked_assets:
            state.data["manual_pause"] = True
            state.data["manual_pause_reason"] = "untracked_account_asset"
            state.data["recovery_required"] = {
                "symbol": None,
                "quantity": None,
                "reason": f"Account contains non-USDT assets not tracked as bot positions: {sorted(untracked_assets)}. Manual reconciliation required.",
                "time": time.time(),
            }
            state.save()
            raise RuntimeError("Startup preflight refused: untracked non-USDT account assets exist.")

        log.info("Account preflight passed: open orders match tracked OCO children and non-USDT assets are tracked.")

    def next_scan_batch(self):
        symbols = self.client.refresh_universe_if_due()
        batch, self.scan_index = rotating_batch(symbols, self.scan_index, SCAN_BATCH_SIZE)
        return batch

    def run(self):
        if state.data.get("manual_pause_reason") == "universe_refresh_or_scan_failure":
            state.data["manual_pause"] = False
            state.data["manual_pause_reason"] = None
            state.save()
            log.info("Cleared transient universe refresh pause on startup.")
        notify("Bot loop active. Use the 'Stop Bot' button on the dashboard to stop; do not delete state file while positions are open.")
        while not stop_event.is_set():
            try:
                # 1. Settle existing positions / check time stops first
                if not LIVE:
                    self.settle_paper_positions()
                else:
                    self.enforce_time_stop()
                    # Reconcile active positions & monitor stop-loss limits on a dedicated 5s cadence
                    if time.time() - self.last_reconcile >= 5:
                        self.reconcile()
                        self.last_reconcile = time.time()
                    # Periodic full account-wide open orders & dust audit every 60s
                    if time.time() - self.last_preflight >= 60:
                        self.startup_preflight()
                        self.last_preflight = time.time()

                # 2. Evaluate fresh balances, mark-to-market equity, and check flat account change
                balances = self.balances()
                equity = None
                try:
                    equity = self.equity_usdt(balances)
                    self.last_equity = equity
                    self.last_balances = balances
                    self.last_refresh_time = time.time()
                    self.check_unrecorded_flat_account_change(equity)
                    self.risk_status(equity)
                    if state.data.get("manual_pause_reason") == "equity_unavailable":
                        state.data["manual_pause"] = False
                        state.data["manual_pause_reason"] = None
                except Exception as eq_err:
                    log.warning("Equity valuation failed (%s); entries locked but exits will continue.", eq_err)
                    state.data["manual_pause"] = True
                    state.data["manual_pause_reason"] = "equity_unavailable"
                    state.save()

                if state.data.get("recovery_required"):
                    state.data["manual_pause"] = True
                    log.critical("New entries blocked: recovery_required is unresolved.")

                # Keep candle tracking and setup tracking running continuously
                try:
                    batch = self.next_scan_batch()
                    for symbol in batch:
                        try:
                            sig = self.signal_for(symbol)
                            can_enter = (
                                sig and equity is not None
                                and not state.data.get("recovery_required")
                                and not state.data.get("manual_pause")
                                and time.time() >= float(state.data.get("paused_until", 0))
                            )
                            if can_enter:
                                self.open_trade(sig, equity, balances)
                        except Exception as e:
                            log.exception("Symbol loop error %s", symbol)
                            notify(f"{symbol}: strategy error; no new trade: {e}")
                    # Auto-clear scan failure lock after a successful scan batch
                    if state.data.get("manual_pause_reason") == "universe_refresh_or_scan_failure":
                        state.data["manual_pause"] = False
                        state.data["manual_pause_reason"] = None
                        state.save()
                except Exception as e:
                    state.data["manual_pause"] = True
                    state.data["manual_pause_reason"] = "universe_refresh_or_scan_failure"
                    state.save()
                    log.exception("Universe refresh/scan failure; entries locked")
                    notify(f"CRITICAL: symbol universe scan failed; entries paused: {e}")

                state.save()
            except Exception as e:
                log.exception("Main loop error"); notify(f"Bot service error; new entries skipped: {e}")
            stop_event.wait(POLL_SECONDS)

bot_instance = None
manual_close_lock = threading.Lock()
_dashboard_price_cache: dict[str, dict[str, Any]] = {}
_dashboard_price_cache_lock = threading.Lock()

_fast_rs_cache: dict[str, Any] = {
    "last_fetch": 0.0,
    "top_leaders": [],
    "major_coins": [],
    "btc_24h": 0.0,
}
_fast_rs_lock = threading.Lock()
MAJOR_SYMBOLS_FOR_RS = ["ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "NEARUSDT", "UNIUSDT", "LINKUSDT", "BEAMXUSDT"]


def _fetch_live_rs_snapshot(client, top_candidate_symbols=None):
    """Fetch lightweight live 24h ticker for top candidates and major symbols (weight=2, payload ~2KB)
    to keep Relative Strength vs BTC dynamic and up-to-the-second without waiting for the full 15m universe scan."""
    now = time.time()
    with _fast_rs_lock:
        if now - _fast_rs_cache["last_fetch"] < 12.0 and _fast_rs_cache["top_leaders"]:
            return _fast_rs_cache

    if client is None:
        return _fast_rs_cache

    known_symbols = list(getattr(client, "symbols", []))
    candidates = list(top_candidate_symbols or [])
    if not candidates and known_symbols:
        raw_rs = getattr(client, "relative_strength", {})
        sorted_leaders = sorted(
            [s for s in known_symbols if s in raw_rs and str(s).isascii() and str(s).isalnum()],
            key=lambda s: -raw_rs.get(s, -999)
        )
        candidates = sorted_leaders[:8]

    target_symbols = list(dict.fromkeys(["BTCUSDT"] + MAJOR_SYMBOLS_FOR_RS + candidates))[:16]

    try:
        encoded_symbols = json.dumps(target_symbols, separators=(",", ":"))
        tickers = client.request("GET", "/api/v3/ticker/24hr", {"symbols": encoded_symbols}, timeout=10)
        if not isinstance(tickers, list):
            return _fast_rs_cache

        btc_row = next((t for t in tickers if t.get("symbol") == "BTCUSDT"), None)
        btc_24h = float(btc_row.get("priceChangePercent", 0.0)) if btc_row else 0.0

        item_map = {}
        for row in tickers:
            sym = str(row.get("symbol", "")).upper()
            try:
                chg = float(row.get("priceChangePercent", 0.0))
                px = float(row.get("lastPrice", 0.0))
                vol = float(row.get("quoteVolume", 0.0))
                rs = round(chg - btc_24h, 2)
                item_map[sym] = {
                    "symbol": sym,
                    "rs": rs,
                    "price": px,
                    "change_24h": round(chg, 2),
                    "btc_change_24h": round(btc_24h, 2),
                    "volume_usdt": round(vol, 2),
                }
                if hasattr(client, "relative_strength"):
                    client.relative_strength[sym] = rs
                if hasattr(client, "latest_prices"):
                    client.latest_prices[sym] = px
            except (ValueError, TypeError):
                continue

        leaders_list = [item_map[s] for s in candidates if s in item_map]
        leaders_list.sort(key=lambda x: -x["rs"])

        major_list = [item_map[s] for s in MAJOR_SYMBOLS_FOR_RS if s in item_map]
        major_list.sort(key=lambda x: -x["rs"])

        with _fast_rs_lock:
            _fast_rs_cache["last_fetch"] = now
            _fast_rs_cache["top_leaders"] = leaders_list
            _fast_rs_cache["major_coins"] = major_list
            _fast_rs_cache["btc_24h"] = btc_24h
    except Exception as exc:
        log.debug("Live RS fast-refresh skipped: %s", exc)

    return _fast_rs_cache


def _dashboard_num(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _dashboard_price_snapshot(symbols):
    result = {}
    now = time.time()
    client = getattr(bot_instance, "client", None)
    latest_prices = getattr(client, "latest_prices", {}) if client is not None else {}
    # Prices come from the 24h ticker snapshot (refreshed every TICKER_REFRESH_SECONDS), so they
    # can be minutes old. Report the real age instead of claiming they are live.
    refreshed_at = _dashboard_num(getattr(client, "last_ticker_refresh_at", 0.0)) if client is not None else None
    age = max(0.0, now - refreshed_at) if refreshed_at else None
    for symbol in symbols:
        price = latest_prices.get(symbol)
        result[symbol] = {
            "price": price,
            "age_seconds": age if price is not None else None,
            "error": None if price is not None else "no ticker price yet",
        }
    return result


def _dashboard_payload():
    # Detach state before formatting so dashboard rendering never mutates trading state.
    with state.lock:
        data = json.loads(json.dumps(state.data))
    positions_raw = data.get("positions", {}) if isinstance(data.get("positions", {}), dict) else {}
    trades_raw = data.get("trades", []) if isinstance(data.get("trades", []), list) else []
    events_raw = data.get("events", []) if isinstance(data.get("events", []), list) else []
    balances = getattr(bot_instance, "last_balances", {}) if bot_instance is not None else {}
    balances = balances if isinstance(balances, dict) else {}
    equity = _dashboard_num(getattr(bot_instance, "last_equity", None)) if bot_instance is not None else None
    updated_at = _dashboard_num(getattr(bot_instance, "last_refresh_time", None)) if bot_instance is not None else None
    client = getattr(bot_instance, "client", None) if bot_instance is not None else None
    scanned_symbols = list(getattr(client, "symbols", [])) if client is not None else []
    eligible_count = len(getattr(client, "all_spot_usdt_symbols", [])) if client is not None else 0
    watch_symbols = list(dict.fromkeys(scanned_symbols[:8] + list(positions_raw.keys())))
    prices = _dashboard_price_snapshot(watch_symbols)

    positions = []
    total_exposure = 0.0
    total_unrealized = 0.0
    total_cost_basis = 0.0
    all_values_marked = True
    all_pnl_marked = True
    for symbol, raw in positions_raw.items():
        if not isinstance(raw, dict):
            continue
        qty = _dashboard_num(raw.get("quantity"))
        entry = _dashboard_num(raw.get("entry_price"))
        mark = _dashboard_num((prices.get(symbol) or {}).get("price"))
        value = mark * qty if mark is not None and qty is not None else None
        pnl = (mark - entry) * qty if mark is not None and entry is not None and qty is not None else None
        pnl_pct = (pnl / (entry * qty) * 100) if pnl is not None and entry and qty else None
        if value is None:
            all_values_marked = False
        else:
            total_exposure += value
        if pnl is None:
            all_pnl_marked = False
        else:
            total_unrealized += pnl
            total_cost_basis += (entry * qty)
        item = dict(raw)
        item.update({
            "symbol": symbol, "quantity": qty, "entry_price": entry,
            "mark_price": mark, "market_value_usdt": value,
            "unrealized_pnl_usdt": pnl, "unrealized_pnl_pct": pnl_pct,
        })
        positions.append(item)
    positions.sort(key=lambda p: float(p.get("opened_at") or 0), reverse=True)
    unrealized = total_unrealized if positions and all_pnl_marked else (0.0 if not positions else None)
    unrealized_pct = (total_unrealized / total_cost_basis * 100) if total_cost_basis > 0 and all_pnl_marked else (0.0 if not positions else None)
    exposure = total_exposure if positions and all_values_marked else (0.0 if not positions else None)
    exposure_pct = (exposure / equity * 100) if exposure is not None and equity and equity > 0 else None

    usdt_bal = balances.get("USDT", (None, None))
    free_usdt = _dashboard_num(usdt_bal[0]) if len(usdt_bal) > 0 else None
    locked_usdt = _dashboard_num(usdt_bal[1]) if len(usdt_bal) > 1 else None
    if free_usdt is None:
        eq_fallback = equity or _dashboard_num(data.get("last_account_equity_usdt")) or 1000.0
        free_usdt = max(0.0, eq_fallback - total_cost_basis)
    flow_adjusted = None
    if equity is not None:
        try:
            flow_adjusted = flow_adjusted_equity(equity, float(data.get("net_external_flows_usdt", 0) or 0))
        except Exception:
            flow_adjusted = None
    day_start = _dashboard_num(data.get("day_start_equity"))
    week_start = _dashboard_num(data.get("week_start_equity"))
    peak = _dashboard_num(data.get("peak_equity"))
    day_change = flow_adjusted - day_start if flow_adjusted is not None and day_start is not None else None
    week_change = flow_adjusted - week_start if flow_adjusted is not None and week_start is not None else None
    day_change_pct = day_change / day_start * 100 if day_change is not None and day_start else None
    week_change_pct = week_change / week_start * 100 if week_change is not None and week_start else None

    terminal_trades = [
        t for t in trades_raw
        if t.get("status") in TERMINAL_TRADE_STATUSES
    ]
    total_closed = len(terminal_trades)
    wins = []
    losses = []
    realized_pnl_total = 0.0
    for t in terminal_trades:
        pnl_val = t.get("pnl")
        if pnl_val is None:
            pnl_val = t.get("gross_realized_pnl_usdt_estimate")
        val = _dashboard_num(pnl_val) or 0.0
        realized_pnl_total += val
        if val > 0:
            wins.append(t)
        else:
            losses.append(t)

    win_rate_pct = round((len(wins) / total_closed * 100), 1) if total_closed > 0 else 0.0
    amount_invested = round(total_cost_basis, 2)

    rs_snapshot = _fetch_live_rs_snapshot(client)
    live_top_rs = rs_snapshot.get("top_leaders") or []
    live_major_rs = rs_snapshot.get("major_coins") or []
    btc_24h = rs_snapshot.get("btc_24h", 0.0)
    rs_age = round(max(0.0, time.time() - rs_snapshot.get("last_fetch", 0.0)), 1)

    return {
        "timestamp": time.time(),
        "live_trading": bool(LIVE),
        "exit_mode": EXIT_MODE,
        "amount_invested_usdt": amount_invested,
        "win_rate_pct": win_rate_pct,
        "total_closed_trades": total_closed,
        "wins_count": len(wins),
        "losses_count": len(losses),
        "btc_regime": getattr(bot_instance, "_cached_btc_regime", (True, "Unknown"))[1] if bot_instance is not None else "Unknown",
        # Real-time Relative Strength vs BTC with live Binance prices and 24h change metrics.
        # Tuple format [sym, rs, price, change_24h, btc_change_24h, volume] guarantees 100% backward
        # compatibility with any cached frontend expecting item[0]=symbol and item[1]=rs.
        "top_relative_strength": [
            [m["symbol"], m["rs"], m.get("price"), m.get("change_24h"), m.get("btc_change_24h"), m.get("volume_usdt")]
            for m in (live_top_rs if live_top_rs else [
                {"symbol": str(s), "rs": float(rs), "price": getattr(client, "latest_prices", {}).get(s), "change_24h": None, "btc_change_24h": btc_24h, "volume_usdt": None}
                for s, rs in sorted(getattr(client, "relative_strength", {}).items(), key=lambda x: -x[1])
                if s in set(getattr(client, "symbols", [])) and str(s).isascii() and str(s).isalnum()
            ][:8])
        ] if client is not None else [],
        "major_relative_strength": [
            [m["symbol"], m["rs"], m.get("price"), m.get("change_24h"), m.get("btc_change_24h"), m.get("volume_usdt")]
            for m in live_major_rs
        ] if client is not None else [],
        "btc_24h_change_pct": btc_24h,
        "rs_updated_at": rs_snapshot.get("last_fetch", 0.0),
        "rs_age_seconds": rs_age,
        "symbols": scanned_symbols[:12],
        "symbol_count": len(scanned_symbols),
        "eligible_symbol_count": eligible_count,
        "scan_batch_size": SCAN_BATCH_SIZE,
        "min_24h_quote_volume_usdt": MIN_24H_QUOTE_VOLUME_USDT,
        "poll_seconds": POLL_SECONDS,
        "bot_data_updated_at": updated_at,
        "equity_usdt": equity,
        "free_usdt": free_usdt,
        "locked_usdt": locked_usdt,
        "positions": positions,
        "prices": prices,
        "total_exposure_usdt": exposure,
        "exposure_pct": exposure_pct,
        "unrealized_pnl_usdt": unrealized,
        "unrealized_pnl_pct": unrealized_pct,
        "realized_pnl_tracked": total_closed > 0,
        "realized_pnl_usdt": round(realized_pnl_total, 2),
        "day_equity_change_usdt": day_change,
        "day_equity_change_pct": day_change_pct,
        "week_equity_change_usdt": week_change,
        "week_equity_change_pct": week_change_pct,
        "day_start_equity_usdt": day_start,
        "week_start_equity_usdt": week_start,
        "peak_equity_usdt": peak,
        "paused_until": _dashboard_num(data.get("paused_until")) or 0,
        "manual_pause": bool(data.get("manual_pause")),
        "manual_pause_reason": data.get("manual_pause_reason"),
        "account_change_requires_review": data.get("account_change_requires_review"),
        "net_external_flows_usdt": _dashboard_num(data.get("net_external_flows_usdt", 0)) or 0,
        "flow_adjusted_equity_usdt": flow_adjusted,
        "external_flow_ledger_count": len(data.get("external_flow_ledger", [])) if isinstance(data.get("external_flow_ledger", []), list) else 0,
        "recovery_required": data.get("recovery_required"),
        "recent_trades": list(reversed(trades_raw[-20:])),
        "recent_events": list(reversed(events_raw[-30:])),
        "dashboard_token": DASHBOARD_TOKEN,
        "risk": {
            "exit_mode": EXIT_MODE,
            "risk_per_trade_pct": RISK_PCT * 100,
            "max_position_pct": MAX_POSITION_PCT * 100,
            "trade_notional_usdt": float(TRADE_NOTIONAL_USDT),
            "take_profit_pct": TAKE_PROFIT_PCT * 100,
            "min_stop_pct": MIN_STOP_PCT * 100,
            "max_stop_pct": MAX_STOP_PCT * 100,
            "max_hold_hours": MAX_HOLD_HOURS,
            "entry_order_timeout_seconds": ENTRY_ORDER_TIMEOUT_SECONDS,
            "max_exposure_pct": MAX_EXPOSURE_PCT * 100,
            "reserve_pct": RESERVE_PCT * 100,
            "daily_loss_pct": DAILY_LOSS_PCT * 100,
            "weekly_loss_pct": WEEKLY_LOSS_PCT * 100,
            "drawdown_pct": DRAWDOWN_PCT * 100,
            "max_positions": MAX_POSITIONS,
        },
    }


class DashboardHandler(BaseHTTPRequestHandler):
    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    def _validate_host(self):
        host_header = self.headers.get("Host", "")
        host = host_header.split(":")[0].strip().lower()
        allowed = {"127.0.0.1", "localhost", str(DASHBOARD_HOST).strip().lower()}
        if host not in allowed:
            self.send_error(403, "Forbidden: Invalid Host header")
            return False
        return True

    def do_POST(self):
        if not self._validate_host():
            return
        if self.path not in ("/close-position", "/stop-bot"):
            self.send_error(404)
            return
        # Drain body first so socket buffer is fully read (prevents Windows TCP RST WinError 10053)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if 0 < length <= 4096 else b""
        except Exception:
            raw_body = b""
        token = self.headers.get("X-Dashboard-Token")
        if not token or token != DASHBOARD_TOKEN:
            self.send_error(403, "Forbidden: Invalid or missing X-Dashboard-Token")
            return
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self.send_error(403, "Local dashboard only")
            return
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}", f"http://localhost:{DASHBOARD_PORT}"):
            self.send_error(403, "Cross-origin requests are not allowed")
            return
        if self.path == "/stop-bot":
            log.info("Clean shutdown requested via Web Dashboard button.")
            notify("Bot stopped via Web Dashboard button.")
            self._json_response(200, {"ok": True, "message": "Bot is stopping cleanly."})
            stop_event.set()
            threading.Timer(0.8, lambda: os._exit(0)).start()
            return
        if not manual_close_lock.acquire(blocking=False):
            self._json_response(409, {"ok": False, "error": "Another manual close request is already processing."})
            return
        try:
            if not raw_body:
                self._json_response(400, {"ok": False, "error": "Invalid request body."})
                return
            try:
                body = json.loads(raw_body.decode("utf-8"))
            except Exception:
                self._json_response(400, {"ok": False, "error": "Malformed JSON."})
                return
            symbol = str(body.get("symbol", "")).upper()
            if not symbol.endswith("USDT") or not symbol[:-4].isalnum():
                self._json_response(400, {"ok": False, "error": "Invalid USDT Spot symbol."})
                return
            if bot_instance is None:
                self._json_response(503, {"ok": False, "error": "Trading bot is not initialized."})
                return
            try:
                result = bot_instance.close_position_at_market(symbol)
                self._json_response(200, result)
            except Exception as exc:
                log.warning("Dashboard manual close refused/failed for %s: %s", symbol, exc)
                self._json_response(409, {"ok": False, "error": str(exc)})
        finally:
            manual_close_lock.release()

    def _json_response(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._validate_host():
            return
        if self.path == "/status":
            body = json.dumps(_dashboard_payload(), separators=(",", ":")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/static/"):
            clean_path = self.path.split("?")[0]
            clean_name = Path(clean_path[8:]).name
            target = ROOT / "static" / clean_name
            if target.exists() and target.is_file() and target.resolve().is_relative_to((ROOT / "static").resolve()):
                body = target.read_bytes()
                self.send_response(200)
                content_type = "application/javascript; charset=utf-8" if target.suffix == ".js" else "text/css; charset=utf-8" if target.suffix == ".css" else "application/octet-stream"
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-cache, must-revalidate")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)
            return
        if self.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        html_path = ROOT / "dashboard.html"
        try:
            body = html_path.read_bytes()
        except OSError:
            body = b"<html><body><h1>Dashboard file missing</h1><p>Restore dashboard.html beside bot.py.</p></body></html>"
            self.send_response(503)
        else:
            self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve_dashboard():
    try:
        ThreadingHTTPServer((DASHBOARD_HOST, DASHBOARD_PORT), DashboardHandler).serve_forever()
    except Exception as e:
        log.error("Dashboard failed to start: %s", e)

def main():
    global bot_instance
    if LIVE:
        if os.getenv("CONFIRM_LIVE_TRADING", "") != "I_UNDERSTAND_REAL_ORDERS":
            raise SystemExit("Live mode requires CONFIRM_LIVE_TRADING=I_UNDERSTAND_REAL_ORDERS in .env")
        if not DATA_FILE.exists():
            raise SystemExit(
                f"{DATA_FILE.name} is missing. Run initialize_allcoin_live_state.py first; "
                "it performs read-only account checks and will refuse unsafe account states."
            )
        if state.data.get("recovery_required"):
            raise SystemExit("Unresolved recovery_required state; reconcile account and orders before starting.")
        log.critical("LIVE SPOT TRADING CANDIDATE STARTING. Real orders can be submitted.")
    else:
        # Signal-Only / Paper mode: clean state initialization for demo
        state.data["manual_pause"] = False
        state.data["recovery_required"] = None
        state.save()
        log.info("SIGNAL-ONLY / PAPER MODE ACTIVE. No real money or exchange orders will be placed.")

    bot_instance = SwingBot()
    bot_instance.startup_preflight()
    if not state.data.get("positions") and state.data.get("last_account_equity_usdt") is None:
        if bot_instance.set_flat_baseline():
            state.save()
    threading.Thread(target=serve_dashboard, daemon=True).start()
    log.info("Dashboard active at: http://%s:%s", DASHBOARD_HOST, DASHBOARD_PORT)
    def shutdown(*_): stop_event.set()
    def ignore_sigint(*_):
        log.warning("Ctrl+C shortcut is disabled. Use the 'Stop Bot' button on the dashboard (http://%s:%s) to stop cleanly.", DASHBOARD_HOST, DASHBOARD_PORT)
    signal.signal(signal.SIGINT, ignore_sigint)
    signal.signal(signal.SIGTERM, shutdown)
    bot_instance.run()

if __name__ == "__main__": main()