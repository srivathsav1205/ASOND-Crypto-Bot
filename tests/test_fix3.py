import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

SCRATCH_DIR = Path("tests")
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_fix3_state.json"
LOG_PATH = SCRATCH_DIR / "test_fix3.log"

if STATE_PATH.exists():
    STATE_PATH.unlink()
if LOG_PATH.exists():
    LOG_PATH.unlink()

os.environ["BOT_STATE_FILE"] = str(STATE_PATH.resolve())
os.environ["BOT_LOG_FILE"] = str(LOG_PATH.resolve())
os.environ["ENABLE_LIVE_TRADING"] = "false"

import bot

def make_candle(open_time_ms, close_time_ms, o=3000.0, h=3010.0, l=2990.0, c=3000.0):
    return [
        open_time_ms,
        str(o),
        str(h),
        str(l),
        str(c),
        "100.0",
        close_time_ms,
        "300000.0",
        500,
        "50.0",
        "150000.0",
        "0"
    ]

def main():
    print("=== STARTING FIX 3 TEST: 24H TIME STOP CANDLE VERIFICATION (BOUNDARY & PRE-BOUNDARY) ===")
    bot.MAX_HOLD_HOURS = 24.0

    test_bot = bot.SwingBot.__new__(bot.SwingBot)
    test_bot.last_hour_close = {}

    current_candles = []
    class MockClient:
        def klines(self, symbol, interval, limit=100, start_time=None):
            if start_time is None:
                return current_candles
            return [c for c in current_candles if int(c[0]) >= start_time or int(c[6]) >= start_time]

    test_bot.client = MockClient()

    t0_ms = 1700000000000
    t0_s = t0_ms / 1000.0
    candle_15m_ms = 15 * 60 * 1000
    open_candle_close_ms = t0_ms + candle_15m_ms - 1

    symbol = "ETHUSDT"
    trade_id = f"paper_{symbol}_{t0_ms}"

    with bot.state.lock:
        bot.state.data["positions"] = {
            symbol: {
                "trade_id": trade_id,
                "symbol": symbol,
                "quantity": "0.01",
                "entry_price": 3000.0,
                "stop_price": 2900.0,
                "target_price": 3200.0,
                "opened_at": t0_s,
                "open_candle_close_time": open_candle_close_ms,
                "paper_checked_close_time": open_candle_close_ms,
                "is_paper": True,
            }
        }
        bot.state.data["trades"] = [{
            "id": trade_id,
            "time": t0_s,
            "symbol": symbol,
            "status": "OPEN",
            "entry": 3000.0,
            "stop": 2900.0,
            "target": 3200.0,
            "qty": "0.01",
            "is_paper": True,
        }]
        bot.state.data["manual_pause"] = False
        bot.state.data["last_account_equity_usdt"] = 1000.0
        bot.state.save()

    # Case 0: Candle where trade opened
    print("[0] Testing candle in which trade opened (excluded from check)...")
    c0 = make_candle(t0_ms, open_candle_close_ms, o=3000, h=3005, l=2995, c=3002)
    current_candles = [c0]
    test_bot._paper_check(symbol, bot.state.data["positions"][symbol])
    assert symbol in bot.state.data["positions"], "Trade should NOT exit in open candle"
    assert bot.state.data["trades"][0]["status"] == "OPEN"
    print("    Open candle safely excluded; trade remains OPEN.")

    # Case 1: Candle 1 hour after open
    print("[1] Testing candle 1 hour after open...")
    t1_open = t0_ms + 3600 * 1000
    c1 = make_candle(t1_open, t1_open + candle_15m_ms - 1, o=3002, h=3010, l=2990, c=3005)
    current_candles = [c1]
    test_bot._paper_check(symbol, bot.state.data["positions"][symbol])
    assert symbol in bot.state.data["positions"]
    assert bot.state.data["trades"][0]["status"] == "OPEN"
    print("    1h candle evaluated; trade remains OPEN.")

    # Case 2: Candle 12 hours after open
    print("[2] Testing candle 12 hours after open...")
    t12_open = t0_ms + 12 * 3600 * 1000
    c12 = make_candle(t12_open, t12_open + candle_15m_ms - 1, o=3005, h=3015, l=2995, c=3008)
    current_candles = [c12]
    test_bot._paper_check(symbol, bot.state.data["positions"][symbol])
    assert symbol in bot.state.data["positions"]
    assert bot.state.data["trades"][0]["status"] == "OPEN"
    print("    12h candle evaluated; trade remains OPEN.")

    # Case 3: Candle 23 hours after open
    print("[3] Testing candle 23 hours after open...")
    t23_open = t0_ms + 23 * 3600 * 1000
    c23 = make_candle(t23_open, t23_open + candle_15m_ms - 1, o=3008, h=3012, l=2998, c=3004)
    current_candles = [c23]
    test_bot._paper_check(symbol, bot.state.data["positions"][symbol])
    assert symbol in bot.state.data["positions"]
    assert bot.state.data["trades"][0]["status"] == "OPEN"
    print("    23h candle evaluated; trade remains OPEN.")

    # Case 4: Pre-boundary candle: 23 hours 45 minutes after open (t0 + 23.75h)
    print("[4] Testing pre-boundary candle 23h45m after open (just before 24h)...")
    t23_45_open = t0_ms + int(23.75 * 3600 * 1000)
    t23_45_close = t23_45_open + candle_15m_ms - 1
    # Verify close time is strictly less than 24h
    assert t23_45_close < t0_ms + 24 * 3600 * 1000
    c23_45 = make_candle(t23_45_open, t23_45_close, o=3004, h=3010, l=2990, c=3002)
    current_candles = [c23_45]
    test_bot._paper_check(symbol, bot.state.data["positions"][symbol])
    assert symbol in bot.state.data["positions"], "Trade MUST NOT exit before 24h boundary (at 23h45m)"
    assert bot.state.data["trades"][0]["status"] == "OPEN"
    print(f"    Pre-boundary candle (ct={t23_45_close} < {t0_ms + 24*3600*1000}) evaluated; trade remains OPEN.")

    # Case 5: Boundary candle: exactly 24 hours after open (ct >= opened_at + 24h)
    print("[5] Testing candle closing at/after 24 hours (ct >= opened_at + 24h)...")
    t24_open = t0_ms + 24 * 3600 * 1000
    t24_close = t24_open + candle_15m_ms - 1
    c24 = make_candle(t24_open, t24_close, o=3002, h=3010, l=2990, c=3006)
    current_candles = [c24]
    test_bot._paper_check(symbol, bot.state.data["positions"][symbol])

    assert symbol not in bot.state.data["positions"], "Trade MUST exit when candle close time >= 24h"
    trade_record = bot.state.data["trades"][0]
    assert trade_record["status"] == "CLOSED_TIME_STOP"
    assert trade_record["exit_reason"] == "TIME_STOP_HIT"
    print(f"    24h candle correctly triggered TIME STOP: status={trade_record['status']}, exit={trade_record['exit']}, pnl={trade_record['pnl']}")

    print("=== FIX 3 TEST PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()