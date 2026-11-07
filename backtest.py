#!/usr/bin/env python3
"""
Single-file Multi-Pair Backtester for Binance Spot Breakout Strategy.
Compares:
  V1: Baseline (Original bot: fixed 3% TP)
  V2: + Dynamic 2R Target
  V3: + BTC 4H Regime Gate (BTC > 4H 50-EMA)
  V4: + Relative Strength (RS > 0.0% vs BTC over 24h)

Models:
  - 12 months historical data (4h, 1h, 15m)
  - Train/Test Split: Months 1-8 (In-Sample), Months 9-12 (Out-of-Sample)
  - 0.1% fee per side (0.2% round-trip)
  - 0.05% entry slippage
  - $20 fixed notional per trade
  - Local disk caching in ./data/ directory to avoid rate limits
"""

from __future__ import annotations
import os, sys, time, json, math, bisect, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# Universe of top liquid USDT spot pairs
DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "SUIUSDT", "DOGEUSDT",
    "XRPUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "NEARUSDT"
]

TRADE_NOTIONAL = 20.0     # $20 trade size
FEE_PCT = 0.001           # 0.1% per side (0.2% round trip)
SLIPPAGE_PCT = 0.0005     # 0.05% entry slippage
STOP_SLIPPAGE = 0.002     # 0.20% stop exit slippage

# Binance Public Data Mirror (fast, no rate-limit IP blocks)
BINANCE_ENDPOINTS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
]

def fetch_klines_chunk(symbol: str, interval: str, start_time: int, end_time: int, limit: int = 1000):
    params = f"symbol={symbol}&interval={interval}&startTime={start_time}&endTime={end_time}&limit={limit}"
    url = f"https://data-api.binance.vision/api/v3/klines?{params}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            if attempt < 4:
                time.sleep(1.0 + attempt * 0.5)
                continue
            raise RuntimeError(f"Failed to fetch {symbol} {interval} chunk starting at {start_time}: {e}")
    return []

def load_or_download_klines(symbol: str, interval: str, months: int = 12) -> list[list]:
    """Download klines with local caching in ./data/{symbol}_{interval}.json, validating cache age."""
    cache_file = DATA_DIR / f"{symbol}_{interval}_{months}m.json"
    max_age_days = float(os.getenv("BACKTEST_CACHE_MAX_AGE_DAYS", "30.0"))
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if data and len(data) > 100:
                last_candle_ms = int(data[-1][6])
                age_days = (time.time() * 1000 - last_candle_ms) / (86400.0 * 1000.0)
                start_dt = datetime.fromtimestamp(data[0][0] / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                end_dt = datetime.fromtimestamp(last_candle_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                if age_days <= max_age_days:
                    print(f"  [Cache hit] {symbol} {interval}: {len(data)} candles loaded (candle age: {age_days:.1f}d, range: {start_dt} to {end_dt})")
                    return data
                else:
                    print(f"  [Cache stale] {symbol} {interval}: last candle is {age_days:.1f}d old (> {max_age_days:.1f}d limit, data end: {end_dt}); re-downloading fresh data...")
        except Exception:
            pass

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(months * 30.5 * 24 * 3600 * 1000)
    print(f"  [Downloading] {symbol} {interval} (last {months} months)...", end="", flush=True)

    all_klines = []
    curr = start_ms
    while curr < now_ms:
        batch = fetch_klines_chunk(symbol, interval, curr, now_ms, limit=1000)
        if not batch:
            break
        all_klines.extend(batch)
        curr = int(batch[-1][6]) + 1
        if len(batch) < 1000:
            break
        time.sleep(0.04)

    # Validate downloaded span before caching to prevent partial caching
    expected_span_ms = int(months * 27 * 24 * 3600 * 1000)
    actual_span_ms = (int(all_klines[-1][6]) - int(all_klines[0][0])) if all_klines else 0
    if not all_klines or actual_span_ms < expected_span_ms:
        raise RuntimeError(f"Download incomplete for {symbol} {interval}: received {len(all_klines)} candles spanning {actual_span_ms / (86400000*30.5):.1f} months, expected {months} months.")

    start_dt = datetime.fromtimestamp(all_klines[0][0] / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    end_dt = datetime.fromtimestamp(all_klines[-1][6] / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cache_file.write_text(json.dumps(all_klines), encoding="utf-8")
    print(f" Done ({len(all_klines)} candles, range: {start_dt} to {end_dt}).")
    return all_klines


# -------------------------------------------------------------
# Mathematical Indicators (Matching bot.py exactly)
# -------------------------------------------------------------

def ema_series(closes: list[float], period: int) -> list[float | None]:
    out = [None] * len(closes)
    if len(closes) < period:
        return out
    k = 2 / (period + 1)
    acc = sum(closes[:period]) / period
    out[period - 1] = acc
    for i in range(period, len(closes)):
        acc = closes[i] * k + acc * (1 - k)
        out[i] = acc
    return out

def atr_single(candles: list[list], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        hi, lo, prev = float(candles[i][2]), float(candles[i][3]), float(candles[i - 1][4])
        trs.append(max(hi - lo, abs(hi - prev), abs(lo - prev)))
    return sum(trs[-period:]) / period


# -------------------------------------------------------------
# Backtest Engine
# -------------------------------------------------------------

class BacktestEngine:
    def __init__(self, symbols: list[str], months: int = 12):
        self.symbols = symbols
        self.months = months
        self.data_4h = {}
        self.data_1h = {}
        self.data_15m = {}
        self.btc_4h = []
        self.btc_4h_ema50 = []
        self.data_1h_close_times = {}
        self.data_15m_start_times = {}
        self._signal_cache = {}

    def load_data(self):
        print(f"\n--- 1. Fetching / Loading Historical Data ({self.months} Months) ---")
        # Load BTC first for regime filtering & benchmark
        self.btc_4h = load_or_download_klines("BTCUSDT", "4h", self.months)
        self.btc_close_times = [int(c[6]) for c in self.btc_4h]
        btc_4h_closes = [float(c[4]) for c in self.btc_4h]
        self.btc_4h_ema50 = ema_series(btc_4h_closes, 50)
        if "BTCUSDT" not in self.data_1h:
            self.data_1h["BTCUSDT"] = load_or_download_klines("BTCUSDT", "1h", self.months)

        for sym in self.symbols:
            print(f"Loading {sym}...")
            self.data_4h[sym] = load_or_download_klines(sym, "4h", self.months)
            self.data_1h[sym] = load_or_download_klines(sym, "1h", self.months)
            self.data_15m[sym] = load_or_download_klines(sym, "15m", self.months)

        # Align all symbols to the common overlapping simulation window up to min_end
        common_end_ms = min(int(self.data_1h[s][-1][6]) for s in self.symbols if self.data_1h.get(s))
        for sym in self.symbols:
            self.data_4h[sym] = [c for c in self.data_4h[sym] if int(c[6]) <= common_end_ms]
            self.data_1h[sym] = [c for c in self.data_1h[sym] if int(c[6]) <= common_end_ms]
            self.data_15m[sym] = [c for c in self.data_15m[sym] if int(c[6]) <= common_end_ms]

        # Precompute lookup index arrays for fast bisect execution
        self.data_1h_close_times = {sym: [int(c[6]) for c in self.data_1h[sym]] for sym in self.symbols if sym in self.data_1h}
        if "BTCUSDT" in self.data_1h:
            self.data_1h_close_times["BTCUSDT"] = [int(c[6]) for c in self.data_1h["BTCUSDT"]]
        self.data_15m_start_times = {sym: [int(c[0]) for c in self.data_15m[sym]] for sym in self.symbols if sym in self.data_15m}

        # Multi-symbol temporal alignment check against BTCUSDT 1H reference series
        ref_btc_1h = self.data_1h.get("BTCUSDT")
        if not ref_btc_1h:
            raise RuntimeError("BTCUSDT 1H data missing; cannot anchor backtest window.")
        btc_last_close = int(ref_btc_1h[-1][6])
        btc_last_dt = datetime.fromtimestamp(btc_last_close / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"\n--- Multi-Symbol Temporal Alignment Check (Reference: BTCUSDT 1H ends {btc_last_dt}) ---")
        for sym in self.symbols:
            h1 = self.data_1h.get(sym)
            if not h1:
                raise RuntimeError(f"Missing 1H data for {sym}")
            sym_last_close = int(h1[-1][6])
            sym_last_dt = datetime.fromtimestamp(sym_last_close / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            diff_ms = abs(sym_last_close - btc_last_close)
            print(f"  {sym:<8} 1H last candle: {sym_last_dt} (diff: {diff_ms // 1000}s)")
            if diff_ms != 0:
                raise RuntimeError(
                    f"Temporal misalignment: {sym} 1H data ends at {sym_last_dt}, but reference BTCUSDT ends at {btc_last_dt} "
                    f"(diff {diff_ms // 1000}s != 0). Synchronize cached klines before backtesting."
                )

    def get_btc_regime(self, timestamp: int) -> bool:
        """Returns True if BTC is above its 4H 50-EMA at the given timestamp. Fails closed (False) on missing data."""
        if not hasattr(self, "btc_close_times") or not self.btc_close_times:
            return False
        # Fast O(log N) precomputed index lookup
        idx = bisect.bisect_right(self.btc_close_times, timestamp) - 1
        if idx >= 50 and idx < len(self.btc_4h_ema50) and self.btc_4h_ema50[idx] is not None:
            close = float(self.btc_4h[idx][4])
            return close > self.btc_4h_ema50[idx]
        return False

    def _get_15m_starts(self, p_sym: str) -> list[int]:
        lookup = getattr(self, "data_15m_start_times", None)
        if lookup is None:
            self.data_15m_start_times = {}
            lookup = self.data_15m_start_times
        if p_sym not in lookup and p_sym in self.data_15m:
            lookup[p_sym] = [int(c[0]) for c in self.data_15m[p_sym]]
        return lookup.get(p_sym, [])

    def get_relative_strength(self, symbol: str, timestamp: int, lookback_hours: int = 24) -> float:
        """Coin return minus BTC return over lookback_hours (matches live bot 24h ticker RS)."""
        ms_window = lookback_hours * 3600 * 1000
        start_ms = timestamp - ms_window

        # Fast O(log N) bisect lookup for coin return
        close_times = getattr(self, "data_1h_close_times", {})
        times = close_times.get(symbol)
        candles = self.data_1h.get(symbol)
        if not times and candles:
            times = [int(c[6]) for c in candles]
        if not times or not candles:
            return 0.0
        idx_now = bisect.bisect_right(times, timestamp) - 1
        idx_then = bisect.bisect_right(times, start_ms) - 1
        if idx_now < 0 or idx_then < 0:
            return 0.0
        px_then = float(candles[idx_then][4])
        if px_then <= 0.0:
            return 0.0
        coin_ret = (float(candles[idx_now][4]) - px_then) / px_then

        # Fast O(log N) bisect lookup for BTC return
        btc_times = close_times.get("BTCUSDT")
        btc_candles = self.data_1h.get("BTCUSDT")
        if not btc_times and btc_candles:
            btc_times = [int(c[6]) for c in btc_candles]
        if not btc_times or not btc_candles:
            return 0.0
        b_idx_now = bisect.bisect_right(btc_times, timestamp) - 1
        b_idx_then = bisect.bisect_right(btc_times, start_ms) - 1
        if b_idx_now < 0 or b_idx_then < 0:
            return 0.0
        b_px_then = float(btc_candles[b_idx_then][4])
        if b_px_then <= 0.0:
            return 0.0
        btc_ret = (float(btc_candles[b_idx_now][4]) - b_px_then) / b_px_then

        return coin_ret - btc_ret

    def get_signals(self, require_15m_conf: bool, is_out_of_sample: bool, sim_start: int, sim_end: int) -> list[dict]:
        if not hasattr(self, "_signal_cache"):
            self._signal_cache = {}
        cache_key = (require_15m_conf, is_out_of_sample)
        if cache_key in self._signal_cache:
            return self._signal_cache[cache_key]

        all_signals = []
        for sym in self.symbols:
            h4 = self.data_4h.get(sym, [])
            h1 = self.data_1h.get(sym, [])
            m15 = self.data_15m.get(sym, [])
            if len(h4) < 210 or len(h1) < 50 or len(m15) < 50:
                continue

            h4_closes = [float(c[4]) for c in h4]
            h4_ema200 = ema_series(h4_closes, 200)

            # Map 4H candles by close time
            h4_idx = 0
            # Track 15m breakouts/retests sequentially
            m15_idx = 0
            m15_setups = {}
            m15_confirmed = {}

            # Iterate 1H candles
            setup_1h = None

            for i in range(30, len(h1)):
                candle_1h = h1[i]
                close_time_1h = int(candle_1h[6])
                if close_time_1h < sim_start or close_time_1h > sim_end:
                    continue

                # 1. Sequential 15m state machine replay
                if require_15m_conf:
                    while m15_idx < len(m15) and int(m15[m15_idx][6]) <= close_time_1h:
                        if m15_idx >= 30:
                            c_15m_candle = m15[m15_idx]
                            m15_time = int(c_15m_candle[6])
                            # Expire stale confirmations (> 4 bars / 60 min)
                            if sym in m15_confirmed and m15_time - int(m15_confirmed[sym]) > 4 * 15 * 60 * 1000:
                                m15_confirmed.pop(sym, None)

                            sub_15m = m15[m15_idx - 29 : m15_idx + 1]
                            highs_15m = [float(x[2]) for x in sub_15m[-21:-1]]
                            level_15m = max(highs_15m)
                            vols_15m = [float(x[5]) for x in sub_15m[-21:-1]]
                            avgvol_15m = sum(vols_15m) / len(vols_15m) if vols_15m else 0.0
                            vol_15m = float(c_15m_candle[5])
                            c_15m = float(c_15m_candle[4])
                            a_15m = atr_single(sub_15m, 14)

                            if a_15m and avgvol_15m > 0:
                                s15 = m15_setups.get(sym)
                                if s15 is None:
                                    if c_15m > level_15m and vol_15m >= 1.5 * avgvol_15m:
                                        m15_setups[sym] = {
                                            "level": level_15m, "bars_waited": 0, "swing_low": float(c_15m_candle[3])
                                        }
                                        m15_confirmed.pop(sym, None)
                                else:
                                    s15["bars_waited"] += 1
                                    s15["swing_low"] = min(s15["swing_low"], float(c_15m_candle[3]))
                                    s15_lev = s15["level"]
                                    near_15 = (float(c_15m_candle[3]) <= s15_lev + 0.25 * a_15m) and (float(c_15m_candle[3]) >= s15_lev - 0.75 * a_15m)
                                    confirms_15 = near_15 and (c_15m > s15_lev) and (c_15m > float(m15[m15_idx - 1][2]))
                                    if s15["bars_waited"] > 6 or c_15m < s15_lev - 0.75 * a_15m:
                                        m15_setups.pop(sym, None)
                                    elif confirms_15:
                                        m15_confirmed[sym] = m15_time
                                        m15_setups.pop(sym, None)
                        m15_idx += 1

                # 2. 4H Trend Filter (Matching bot.py lines 957-963, no lookahead)
                while h4_idx + 1 < len(h4) and int(h4[h4_idx + 1][6]) <= close_time_1h:
                    h4_idx += 1
                if int(h4[h4_idx][6]) > close_time_1h:
                    continue
                if h4_idx < 205 or h4_ema200[h4_idx] is None or h4_ema200[h4_idx - 5] is None:
                    continue
                e_now = h4_ema200[h4_idx]
                e_then = h4_ema200[h4_idx - 5]
                h4_close = float(h4[h4_idx][4])
                if h4_close <= e_now or e_now <= e_then:
                    setup_1h = None
                    continue

                # 3. 1H Setup & Retest (Matching bot.py lines 780-845)
                highs_1h = [float(x[2]) for x in h1[i-20:i]]
                level_1h = max(highs_1h)
                vols_1h = [float(x[5]) for x in h1[i-20:i]]
                avgvol_1h = sum(vols_1h) / len(vols_1h)
                vol_1h = float(candle_1h[5])
                cclose_1h = float(candle_1h[4])
                a_1h = atr_single(h1[i-29:i+1], 14)
                if not a_1h or avgvol_1h <= 0:
                    continue

                if setup_1h is None:
                    if cclose_1h > level_1h and vol_1h >= 1.5 * avgvol_1h:
                        setup_1h = {
                            "level": level_1h, "bars_waited": 0, "swing_low": float(candle_1h[3])
                        }
                    continue

                setup_1h["bars_waited"] += 1
                setup_1h["swing_low"] = min(setup_1h["swing_low"], float(candle_1h[3]))
                lev = setup_1h["level"]
                # Retest pullback touch condition (candle_1h[3] <= lev + 0.25*a_1h) removed per user instruction
                confirms = (cclose_1h > lev) and (cclose_1h > float(h1[i-1][2]))

                if setup_1h["bars_waited"] > 6 or cclose_1h < lev - 0.75 * a_1h:
                    setup_1h = None
                    continue

                if confirms:
                    # Check 15m confirmation within last 4 bars (1 hour) if required
                    if require_15m_conf:
                        conf_15m_time = m15_confirmed.get(sym)
                        if not conf_15m_time or (close_time_1h - conf_15m_time > 4 * 15 * 60 * 1000):
                            setup_1h = None
                            continue

                    bars_waited = setup_1h["bars_waited"]
                    swing_low = setup_1h["swing_low"]
                    stop = swing_low - 0.2 * a_1h
                    entry = cclose_1h
                    dist = entry - stop
                    setup_1h = None

                    if dist <= 0:
                        continue

                    all_signals.append({
                        "symbol": sym,
                        "time": close_time_1h,
                        "entry": entry,
                        "stop": stop,
                        "dist": dist,
                        "1h_idx": i,
                        "vol": vol_1h,
                        "avgvol": avgvol_1h,
                        "bars_waited": bars_waited,
                    })

        all_signals.sort(key=lambda s: s["time"])
        self._signal_cache[cache_key] = all_signals
        return all_signals

    def run_strategy(
        self,
        version: str = "sweep",
        is_out_of_sample: bool = False,
        slippage_mult: float = 1.0,
        tp_pct: float = 0.03,
        require_15m_conf: bool = True,
        has_time_stop: bool = False,
        use_btc_gate: bool = True,
        use_rs_filter: bool = True,
        target_mode: str = "fixed",
        min_stop_pct: float = 0.01,
        max_stop_pct: float = 0.02,
        min_scorecard_points: int = 0,
    ):
        """
        Runs strategy simulation with parameter control.
        Supports legacy version names and arbitrary parameter sweeps.
        """
        if version == "V1_baseline":
            tp_pct = 0.03
            require_15m_conf = True
            has_time_stop = True
            use_btc_gate = False
            use_rs_filter = False
            target_mode = "fixed"
            min_stop_pct = 0.01
            max_stop_pct = 0.04
            min_scorecard_points = 0
        elif version == "V2_2R":
            require_15m_conf = True
            has_time_stop = True
            use_btc_gate = False
            use_rs_filter = False
            target_mode = "2R"
            min_stop_pct = 0.01
            max_stop_pct = 0.04
            min_scorecard_points = 0
        elif version == "V3_btc_gate":
            require_15m_conf = True
            has_time_stop = True
            use_btc_gate = True
            use_rs_filter = False
            target_mode = "2R"
            min_stop_pct = 0.01
            max_stop_pct = 0.04
            min_scorecard_points = 0
        elif version == "V4_relative_strength":
            require_15m_conf = True
            has_time_stop = True
            use_btc_gate = True
            use_rs_filter = True
            target_mode = "2R"
            min_stop_pct = 0.01
            max_stop_pct = 0.04
            min_scorecard_points = 0
        elif version == "V_current_bot":
            tp_pct = 0.03
            require_15m_conf = True
            has_time_stop = False
            use_btc_gate = True
            use_rs_filter = True
            target_mode = "fixed"
            min_stop_pct = 0.01
            max_stop_pct = 0.04
            min_scorecard_points = 0
        elif version == "V_current_with_time_stop":
            tp_pct = 0.03
            require_15m_conf = True
            has_time_stop = True
            use_btc_gate = True
            use_rs_filter = True
            target_mode = "fixed"
            min_stop_pct = 0.01
            max_stop_pct = 0.04
            min_scorecard_points = 0

        trades = []

        # Anchor simulation window strictly to BTCUSDT 1H reference series (with fallback for isolated single-pair tests)
        btc_1h = self.data_1h.get("BTCUSDT")
        if not btc_1h:
            for sym in self.symbols:
                if self.data_1h.get(sym):
                    btc_1h = self.data_1h[sym]
                    break
        if not btc_1h and self.data_1h:
            btc_1h = next(iter(self.data_1h.values()))
        if not btc_1h:
            raise RuntimeError("Reference 1H data missing for strategy anchoring.")
        earliest_ts = int(btc_1h[0][0])
        latest_ts = int(btc_1h[-1][6])

        # Split: 8 months in-sample, 4 months out-of-sample
        total_span = latest_ts - earliest_ts
        split_ts = earliest_ts + int(total_span * (8.0 / 12.0))

        sim_start = split_ts if is_out_of_sample else earliest_ts
        sim_end = latest_ts if is_out_of_sample else split_ts

        # Fetch signals (cached for instant parameter sweep passes)
        all_signals = self.get_signals(require_15m_conf, is_out_of_sample, sim_start, sim_end)

        # ---------------------------------------------------------
        # Portfolio Execution & Filters (Chronological Event Simulation)
        # ---------------------------------------------------------
        open_positions = []  # active portfolio positions
        entry_slip = SLIPPAGE_PCT * slippage_mult
        exit_slip = SLIPPAGE_PCT * slippage_mult
        stop_slip = STOP_SLIPPAGE * slippage_mult
        boundary_crossings = 0

        for sig in all_signals:
            sym = sig["symbol"]
            ts = sig["time"]

            # Filter: Stop distance guardrail (e.g. MIN_STOP_PCT 1.0% to MAX_STOP_PCT 2.0%)
            stop_pct = sig["dist"] / sig["entry"]
            if stop_pct < min_stop_pct or stop_pct > max_stop_pct:
                continue

            # Filter V3: BTC Regime Gate
            btc_healthy = self.get_btc_regime(ts)
            if use_btc_gate:
                if not btc_healthy:
                    continue

            # Filter V4: Relative Strength vs BTC (Matches live bot 24h ticker RS > 0)
            rs = self.get_relative_strength(sym, ts, lookback_hours=24)
            if use_rs_filter:
                if rs <= 0.0:
                    continue

            # Filter: 100-Point Scorecard (Matches bot by gemini.py lines 1389-1399)
            if min_scorecard_points > 0:
                s_btc = 25 if btc_healthy else 0
                s_rs = 25 if rs > 0.0 else (10 if rs > -3.0 else 0)
                s_vol = 20 if sig.get("vol", 0.0) >= 2.0 * sig.get("avgvol", 1.0) else 15
                s_ret = 20 if sig.get("bars_waited", 0) <= 3 else 15
                s_risk = 10 if stop_pct <= 0.025 else 5
                score = s_btc + s_rs + s_vol + s_ret + s_risk
                if score < min_scorecard_points:
                    continue

            # Check for exits of existing positions up to this signal's timestamp
            remaining_positions = []
            for pos in open_positions:
                p_sym = pos["symbol"]
                m15_starts = self._get_15m_starts(p_sym)
                i_start = bisect.bisect_right(m15_starts, pos["last_eval_ts"])
                i_end = bisect.bisect_right(m15_starts, ts)
                p_candles = self.data_15m[p_sym][i_start:i_end] if m15_starts else []
                closed = False
                for candle in p_candles:
                    pos["bars_held"] += 1
                    pos["last_eval_ts"] = int(candle[0])
                    c_low = float(candle[3])
                    c_high = float(candle[2])

                    if c_low <= pos["stop"]:
                        pos["outcome"] = "STOP"
                        pos["exit_price"] = min(pos["stop"], float(candle[1])) * (1.0 - stop_slip)
                        pos["exit_time"] = int(candle[6])
                        closed = True
                        break
                    elif c_high >= pos["target"]:
                        pos["outcome"] = "TARGET"
                        pos["exit_price"] = pos["target"]  # Maker limit exit assumption (0 slippage)
                        pos["exit_time"] = int(candle[6])
                        closed = True
                        break
                    elif has_time_stop and pos["bars_held"] >= 96:  # 24h Time Stop (matches live MAX_HOLD_HOURS)
                        pos["outcome"] = "TIME_EXIT"
                        pos["exit_price"] = float(candle[4]) * (1.0 - exit_slip)
                        pos["exit_time"] = int(candle[6])
                        closed = True
                        break

                if closed:
                    q = TRADE_NOTIONAL / pos["real_entry"]
                    e_cost = q * pos["real_entry"]
                    x_val = q * pos["exit_price"]
                    fees = (e_cost + x_val) * FEE_PCT
                    pnl = (x_val - e_cost) - fees
                    r_mult = pnl / (q * pos["dist"]) if pos["dist"] > 0 else 0.0
                    # Track trades crossing the in-sample boundary
                    if not is_out_of_sample and pos["exit_time"] > sim_end:
                        boundary_crossings += 1
                    else:
                        trades.append({
                            "symbol": p_sym, "time": pos["open_time"], "exit_time": pos["exit_time"],
                            "outcome": pos["outcome"], "pnl": pnl, "r": r_mult, "bars": pos["bars_held"]
                        })
                else:
                    remaining_positions.append(pos)
            open_positions = remaining_positions

            # Portfolio Constraints:
            # 1. MAX_OPEN_POSITIONS = 5
            if len(open_positions) >= 5:
                continue
            # 2. One position per symbol
            if any(p["symbol"] == sym for p in open_positions):
                continue

            # Model Entry
            real_entry = sig["entry"] * (1.0 + entry_slip)
            actual_stop = min(sig["stop"], real_entry * (1 - 0.001))
            real_dist = real_entry - actual_stop

            if target_mode == "fixed":
                target = real_entry * (1.0 + tp_pct)
            else:
                target = real_entry + 2.0 * real_dist  # Dynamic 2R target

            open_positions.append({
                "symbol": sym,
                "open_time": ts,
                "last_eval_ts": ts,
                "real_entry": real_entry,
                "stop": actual_stop,
                "target": target,
                "dist": real_dist,
                "bars_held": 0,
                "outcome": None,
                "exit_price": None,
                "exit_time": None
            })

        # Settle any remaining open positions at the end of the simulation period
        for pos in open_positions:
            p_sym = pos["symbol"]
            m15_starts = self._get_15m_starts(p_sym)
            i_start = bisect.bisect_right(m15_starts, pos["last_eval_ts"])
            i_end = bisect.bisect_right(m15_starts, sim_end)
            p_candles = self.data_15m[p_sym][i_start:i_end] if m15_starts else []
            closed = False
            for candle in p_candles:
                pos["bars_held"] += 1
                pos["last_eval_ts"] = int(candle[0])
                c_low = float(candle[3])
                c_high = float(candle[2])

                if c_low <= pos["stop"]:
                    pos["outcome"] = "STOP"
                    pos["exit_price"] = min(pos["stop"], float(candle[1])) * (1.0 - stop_slip)
                    pos["exit_time"] = int(candle[6])
                    closed = True
                    break
                elif c_high >= pos["target"]:
                    pos["outcome"] = "TARGET"
                    pos["exit_price"] = pos["target"]  # Maker limit exit assumption (0 slippage)
                    pos["exit_time"] = int(candle[6])
                    closed = True
                    break
                elif has_time_stop and pos["bars_held"] >= 96:
                    pos["outcome"] = "TIME_EXIT"
                    pos["exit_price"] = float(candle[4]) * (1.0 - exit_slip)
                    pos["exit_time"] = int(candle[6])
                    closed = True
                    break

            if closed:
                q = TRADE_NOTIONAL / pos["real_entry"]
                e_cost = q * pos["real_entry"]
                x_val = q * pos["exit_price"]
                fees = (e_cost + x_val) * FEE_PCT
                pnl = (x_val - e_cost) - fees
                r_mult = pnl / (q * pos["dist"]) if pos["dist"] > 0 else 0.0
                if not is_out_of_sample and pos["exit_time"] > sim_end:
                    boundary_crossings += 1
                else:
                    trades.append({
                        "symbol": p_sym, "time": pos["open_time"], "exit_time": pos["exit_time"],
                        "outcome": pos["outcome"], "pnl": pnl, "r": r_mult, "bars": pos["bars_held"]
                    })

        # Sort closed trades chronologically by exit time
        trades.sort(key=lambda t: t["exit_time"])
        metrics = self.compute_metrics(trades)
        metrics["start_date"] = datetime.fromtimestamp(sim_start / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        metrics["end_date"] = datetime.fromtimestamp(sim_end / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        metrics["boundary_crossings"] = boundary_crossings
        metrics["trades_list"] = trades
        unclosed = [p for p in open_positions if not p.get("outcome")]
        metrics["unclosed_at_end"] = len(unclosed)
        unrealized_sum = 0.0
        for p in unclosed:
            p_sym = p["symbol"]
            m15_starts = self._get_15m_starts(p_sym)
            i_end = bisect.bisect_right(m15_starts, sim_end)
            last_px = float(self.data_15m[p_sym][i_end - 1][4]) if (i_end > 0 and self.data_15m.get(p_sym)) else p["real_entry"]
            q = TRADE_NOTIONAL / p["real_entry"]
            unrealized_sum += (last_px - p["real_entry"]) * q
        metrics["unrealized_pnl_at_end"] = round(unrealized_sum, 2)
        return metrics

    def compute_metrics(self, trades: list[dict]) -> dict:
        if not trades:
            return {
                "trades": 0, "win_rate": 0.0, "total_pnl": 0.0,
                "profit_factor": 0.0, "max_dd_pct": 0.0, "avg_r": 0.0
            }

        total_trades = len(trades)
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        win_rate = (len(wins) / total_trades) * 100.0

        gross_profit = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

        total_pnl = sum(t["pnl"] for t in trades)
        avg_r = sum(t["r"] for t in trades) / total_trades

        # Compute drawdown curve
        equity = 100.0 # Starting benchmark $100
        peak = equity
        max_dd = 0.0
        for t in trades:
            equity += t["pnl"]
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd

        return {
            "trades": total_trades,
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "profit_factor": round(profit_factor, 2) if not math.isinf(profit_factor) else float("inf"),
            "max_dd_pct": round(max_dd * 100.0, 1),
            "avg_r": round(avg_r, 2)
        }


def print_comparison_table(results_in: dict, results_out: dict, results_sens: dict | None = None):
    print("\n" + "=" * 138)
    print("                                             BACKTEST BENCHMARK RESULTS")
    print("=" * 138)
    print(f"{'Version':<40} | {'Trades':<7} | {'Win %':<6} | {'P.Factor':<8} | {'MaxDD %':<7} | {'Net PnL ($)':<11} | {'BoundCross':<10} | {'Unclosed (Unrealized)':<22}")
    print("-" * 138)

    def _fmt_pf(val):
        return "inf" if (isinstance(val, (int, float)) and math.isinf(val)) else str(val)

    in_sample_range = ""
    for r in results_in.values():
        if "start_date" in r and "end_date" in r:
            in_sample_range = f" ({r['start_date']} to {r['end_date']})"
            break
    print(f"  [IN-SAMPLE: Months 1 to 8 (Tuning / Benchmark){in_sample_range}]")
    for v_name, res in results_in.items():
        bc = res.get("boundary_crossings", 0)
        uc = res.get("unclosed_at_end", 0)
        unreal = res.get("unrealized_pnl_at_end", 0.0)
        uc_str = f"{uc} (${unreal:+.2f})" if uc > 0 else "0 ($0.00)"
        print(f"  {v_name:<38} | {res['trades']:<7} | {res['win_rate']:<6} | {_fmt_pf(res['profit_factor']):<8} | {res['max_dd_pct']:<7} | ${res['total_pnl']:<10} | {bc:<10} | {uc_str:<22}")

    print("-" * 138)
    out_sample_range = ""
    for r in results_out.values():
        if "start_date" in r and "end_date" in r:
            out_sample_range = f" ({r['start_date']} to {r['end_date']})"
            break
    print(f"  [OUT-OF-SAMPLE: Months 9 to 12 (UNSEEN HELD-OUT TEST){out_sample_range}]")
    for v_name, res in results_out.items():
        bc = res.get("boundary_crossings", 0)
        uc = res.get("unclosed_at_end", 0)
        unreal = res.get("unrealized_pnl_at_end", 0.0)
        uc_str = f"{uc} (${unreal:+.2f})" if uc > 0 else "0 ($0.00)"
        print(f"  {v_name:<38} | {res['trades']:<7} | {res['win_rate']:<6} | {_fmt_pf(res['profit_factor']):<8} | {res['max_dd_pct']:<7} | ${res['total_pnl']:<10} | {bc:<10} | {uc_str:<22}")

    if results_sens:
        print("-" * 138)
        print("  [SENSITIVITY PASS: Out-of-Sample with 2.5x Slippage Penalty]")
        for v_name, res in results_sens.items():
            bc = res.get("boundary_crossings", 0)
            uc = res.get("unclosed_at_end", 0)
            unreal = res.get("unrealized_pnl_at_end", 0.0)
            uc_str = f"{uc} (${unreal:+.2f})" if uc > 0 else "0 ($0.00)"
            print(f"  {v_name:<38} | {res['trades']:<7} | {res['win_rate']:<6} | {_fmt_pf(res['profit_factor']):<8} | {res['max_dd_pct']:<7} | ${res['total_pnl']:<10} | {bc:<10} | {uc_str:<22}")
    print("=" * 138)

    print("\n[PORTFOLIO MODELING CONSTRAINTS]")
    print("  * Trade Notional        : $20.00 per trade")
    print("  * Max Open Positions    : 5 positions ($100 max exposure cap)")
    print("  * Capital & Exposure    : $20 fixed trade notional with 5 max concurrent positions ($100 max exposure cap).")
    print("  * Execution Realism     : Entry slippage 0.05%, Stop exit slippage 0.20%, Maker/taker fees 0.10% per side.")
    print("  * Statistical Rigor     : Held-out sample size < 100 trades is marked INCONCLUSIVE (cannot establish edge).\n")

    # Sample size verification for claiming edge
    for v_name, res in results_out.items():
        trades_count = res.get("trades", 0)
        if trades_count < 100:
            print(f"  [!] {v_name}: Out-of-sample trades={trades_count} -> Sample size is INCONCLUSIVE (<100 trades; insufficient statistical power).")


def print_coin_breakdown(trades: list[dict], title: str = "Coin Performance Breakdown"):
    if not trades:
        print(f"\nNo trades recorded for {title}.")
        return

    stats = {}
    for t in trades:
        sym = t["symbol"]
        if sym not in stats:
            stats[sym] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        stats[sym]["trades"] += 1
        if t["pnl"] > 0:
            stats[sym]["wins"] += 1
        else:
            stats[sym]["losses"] += 1
        stats[sym]["pnl"] += t["pnl"]

    sorted_coins = sorted(stats.items(), key=lambda item: item[1]["pnl"], reverse=True)

    print("\n" + "=" * 88)
    print(f"       COIN-LEVEL PERFORMANCE BREAKDOWN: {title}")
    print("=" * 88)
    print(f"{'Coin':<12} | {'Trades':<8} | {'Win Rate':<10} | {'Wins':<6} | {'Losses':<6} | {'Net PnL ($)':<12}")
    print("-" * 88)

    print("  [TOP 5 BEST-PERFORMING COINS]")
    for sym, s in sorted_coins[:5]:
        wr = (s["wins"] / s["trades"] * 100.0) if s["trades"] > 0 else 0.0
        print(f"  {sym:<10} | {s['trades']:<8} | {wr:5.1f}%    | {s['wins']:<6} | {s['losses']:<6} | ${s['pnl']:+8.2f}")

    print("-" * 88)
    print("  [BOTTOM 5 WORST-PERFORMING COINS (DRAG CANDIDATES)]")
    for sym, s in sorted_coins[-5:]:
        wr = (s["wins"] / s["trades"] * 100.0) if s["trades"] > 0 else 0.0
        print(f"  {sym:<10} | {s['trades']:<8} | {wr:5.1f}%    | {s['wins']:<6} | {s['losses']:<6} | ${s['pnl']:+8.2f}")
    print("=" * 88)


if __name__ == "__main__":
    symbols = DEFAULT_SYMBOLS
    run_benchmark = False

    args = sys.argv[1:]
    for arg in args:
        a = arg.strip().lower()
        if a in ("all", "--all"):
            found = set()
            for f in DATA_DIR.glob("*_15m_*.json"):
                sym = f.name.split("_")[0]
                if sym.endswith("USDT"):
                    found.add(sym)
            symbols = sorted(list(found)) if found else DEFAULT_SYMBOLS
        elif a in ("benchmark", "--benchmark"):
            run_benchmark = True
        elif not a.startswith("-"):
            symbols = [s.strip().upper() for s in arg.split(",") if s.strip()]

    engine = BacktestEngine(symbols=symbols, months=12)
    engine.load_data()

    in_sample_results = {}
    out_sample_results = {}
    sens_results = {}

    if run_benchmark:
        print("\n--- 2. Running Simulations Across Versions ---")
        versions = [
            ("V1: Baseline (Fixed 3%)", "V1_baseline"),
            ("V2: + Dynamic 2R Target", "V2_2R"),
            ("V3: + BTC Regime Gate", "V3_btc_gate"),
            ("V4: + Relative Strength (2R)", "V4_relative_strength"),
            ("Current Bot (3% TP + No Time Stop)", "V_current_bot"),
            ("Current Bot (3% TP + 24h Time Stop)", "V_current_with_time_stop"),
        ]
        for label, v_code in versions:
            print(f"Simulating {label}...")
            in_sample_results[label] = engine.run_strategy(v_code, is_out_of_sample=False)
            out_sample_results[label] = engine.run_strategy(v_code, is_out_of_sample=True)

        print("Simulating V4 Sensitivity (2.5x Slippage)...")
        sens_results["V4 (2.5x Slippage)"] = engine.run_strategy("V4_relative_strength", is_out_of_sample=True, slippage_mult=2.5)

    else:
        print("\n--- 2. Running Targeted Sweep: High-Conviction Scorecard & Stop Guard Sweet Spot ---")
        sweep_configs = [
            # Group 1: Stop Guard 1.0% - 2.0%
            ("15m OFF | TP 2.0% | Stop 1.0-2.0% | Sc 0",    0.020, 0.010, 0.020, 0),
            ("15m OFF | TP 2.0% | Stop 1.0-2.0% | Sc 95",   0.020, 0.010, 0.020, 95),
            ("15m OFF | TP 2.0% | Stop 1.0-2.0% | Sc 100",  0.020, 0.010, 0.020, 100),
            ("15m OFF | TP 2.5% | Stop 1.0-2.0% | Sc 0",    0.025, 0.010, 0.020, 0),
            ("15m OFF | TP 2.5% | Stop 1.0-2.0% | Sc 95",   0.025, 0.010, 0.020, 95),
            ("15m OFF | TP 2.5% | Stop 1.0-2.0% | Sc 100",  0.025, 0.010, 0.020, 100),
            ("15m OFF | TP 3.0% | Stop 1.0-2.0% | Sc 0",    0.030, 0.010, 0.020, 0),
            ("15m OFF | TP 3.0% | Stop 1.0-2.0% | Sc 95",   0.030, 0.010, 0.020, 95),
            ("15m OFF | TP 3.0% | Stop 1.0-2.0% | Sc 100",  0.030, 0.010, 0.020, 100),

            # Group 2: Stop Guard 1.5% - 2.5%
            ("15m OFF | TP 2.0% | Stop 1.5-2.5% | Sc 0",    0.020, 0.015, 0.025, 0),
            ("15m OFF | TP 2.0% | Stop 1.5-2.5% | Sc 95",   0.020, 0.015, 0.025, 95),
            ("15m OFF | TP 2.0% | Stop 1.5-2.5% | Sc 100",  0.020, 0.015, 0.025, 100),
            ("15m OFF | TP 2.5% | Stop 1.5-2.5% | Sc 0",    0.025, 0.015, 0.025, 0),
            ("15m OFF | TP 2.5% | Stop 1.5-2.5% | Sc 95",   0.025, 0.015, 0.025, 95),
            ("15m OFF | TP 2.5% | Stop 1.5-2.5% | Sc 100",  0.025, 0.015, 0.025, 100),
            ("15m OFF | TP 3.0% | Stop 1.5-2.5% | Sc 0",    0.030, 0.015, 0.025, 0),
            ("15m OFF | TP 3.0% | Stop 1.5-2.5% | Sc 95",   0.030, 0.015, 0.025, 95),
            ("15m OFF | TP 3.0% | Stop 1.5-2.5% | Sc 100",  0.030, 0.015, 0.025, 100),

            # Benchmark Reference
            ("Benchmark (TP 3% | Stop 1.0-4.0% | Sc 0)",    0.030, 0.010, 0.040, 0),
        ]

        for label, tp_pct, min_s, max_s, min_sc in sweep_configs:
            print(f"Simulating {label}...")
            in_sample_results[label] = engine.run_strategy(
                version="sweep",
                is_out_of_sample=False,
                tp_pct=tp_pct,
                require_15m_conf=False,
                has_time_stop=False,
                use_btc_gate=True,
                use_rs_filter=True,
                target_mode="fixed",
                min_stop_pct=min_s,
                max_stop_pct=max_s,
                min_scorecard_points=min_sc,
            )
            out_sample_results[label] = engine.run_strategy(
                version="sweep",
                is_out_of_sample=True,
                tp_pct=tp_pct,
                require_15m_conf=False,
                has_time_stop=False,
                use_btc_gate=True,
                use_rs_filter=True,
                target_mode="fixed",
                min_stop_pct=min_s,
                max_stop_pct=max_s,
                min_scorecard_points=min_sc,
            )
            sens_results[f"{label} (2.5x Slip)"] = engine.run_strategy(
                version="sweep",
                is_out_of_sample=True,
                slippage_mult=2.5,
                tp_pct=tp_pct,
                require_15m_conf=False,
                has_time_stop=False,
                use_btc_gate=True,
                use_rs_filter=True,
                target_mode="fixed",
                min_stop_pct=min_s,
                max_stop_pct=max_s,
                min_scorecard_points=min_sc,
            )

    print_comparison_table(in_sample_results, out_sample_results, sens_results)

    # Automatically identify top performers and print Coin Breakdown
    valid_configs = [k for k, v in out_sample_results.items() if v.get("trades", 0) > 0]
    if valid_configs:
        best_config = max(
            valid_configs,
            key=lambda k: (out_sample_results[k]["profit_factor"], out_sample_results[k]["total_pnl"])
        )
        print_coin_breakdown(out_sample_results[best_config].get("trades_list", []), title=f"BEST CONFIG: {best_config}")
