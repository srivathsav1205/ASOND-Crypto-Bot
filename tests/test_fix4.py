import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

SCRATCH_DIR = Path("tests")
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_fix4_state.json"
LOG_PATH = SCRATCH_DIR / "test_fix4.log"

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
    print("=== STARTING FIX 4 TEST: UNIFIED PAPER SETTLEMENT SCHEMA & GUARDS ===")
    bot.MAX_HOLD_HOURS = 24.0

    test_bot = bot.SwingBot.__new__(bot.SwingBot)
    test_bot.last_hour_close = {}

    current_candles = []
    current_price = 3000.0

    class MockClient:
        def klines(self, symbol, interval, limit=100, start_time=None):
            if start_time is None:
                return current_candles
            return [c for c in current_candles if int(c[0]) >= start_time or int(c[6]) >= start_time]
        def price(self, symbol):
            return current_price

    test_bot.client = MockClient()

    # Guard: assert balances() or set_flat_baseline() are NEVER called during paper settlement
    def forbidden_balances(*args, **kwargs):
        raise AssertionError("balances() was called during paper settlement! Fix 4 strictly forbids this.")
    def forbidden_rebaseline(*args, **kwargs):
        raise AssertionError("set_flat_baseline() was called during paper settlement! Fix 4 strictly forbids this.")

    test_bot.balances = forbidden_balances
    test_bot.set_flat_baseline = forbidden_rebaseline

    # Helper to set up an open position
    def setup_open_trade(sym, entry=3000.0, stop=2900.0, target=3200.0, opened_ms=1700000000000):
        t_id = f"paper_{sym}_{int(time.time() * 1000)}"
        opened_s = opened_ms / 1000.0
        open_candle_close = opened_ms + 15 * 60 * 1000 - 1
        with bot.state.lock:
            bot.state.data["positions"][sym] = {
                "trade_id": t_id,
                "symbol": sym,
                "quantity": "0.01",
                "entry_price": entry,
                "stop_price": stop,
                "target_price": target,
                "opened_at": opened_s,
                "open_candle_close_time": open_candle_close,
                "paper_checked_close_time": open_candle_close,
                "is_paper": True,
            }
            bot.state.data["trades"].append({
                "id": t_id,
                "time": opened_s,
                "symbol": sym,
                "status": "OPEN",
                "entry": entry,
                "stop": stop,
                "target": target,
                "qty": "0.01",
                "score": 80,
                "score_desc": "Score 80/100",
                "is_paper": True,
            })
            bot.state.save()
        return t_id, open_candle_close

    # Reset state
    with bot.state.lock:
        bot.state.data["positions"] = {}
        bot.state.data["trades"] = []
        bot.state.data["manual_pause"] = False
        bot.state.data["last_account_equity_usdt"] = 1000.0
        bot.state.save()

    # Path 1: Target Hit
    print("[1] Executing Path 1: Target Hit via _paper_check...")
    t0_ms = 1700000000000
    id1, occ1 = setup_open_trade("ETHUSDT", opened_ms=t0_ms)
    c1 = make_candle(occ1 + 1, occ1 + 15 * 60 * 1000, o=3100, h=3250, l=3050, c=3220)
    current_candles = [c1]
    test_bot._paper_check("ETHUSDT", bot.state.data["positions"]["ETHUSDT"])
    assert "ETHUSDT" not in bot.state.data["positions"], "Position should be popped"
    rec1 = bot.state.data["trades"][-1]
    assert rec1["status"] == "CLOSED_TARGET"
    assert rec1["exit_reason"] == "TARGET_HIT"
    print(f"    Path 1 exited: status={rec1['status']}, pnl={rec1['pnl']}")

    # Path 2: Stop Hit
    print("[2] Executing Path 2: Stop Hit via _paper_check...")
    id2, occ2 = setup_open_trade("SOLUSDT", opened_ms=t0_ms)
    c2 = make_candle(occ2 + 1, occ2 + 15 * 60 * 1000, o=2950, h=2960, l=2850, c=2870)
    current_candles = [c2]
    test_bot._paper_check("SOLUSDT", bot.state.data["positions"]["SOLUSDT"])
    assert "SOLUSDT" not in bot.state.data["positions"], "Position should be popped"
    rec2 = bot.state.data["trades"][-1]
    assert rec2["status"] == "CLOSED_STOP"
    assert rec2["exit_reason"] == "STOP_HIT"
    print(f"    Path 2 exited: status={rec2['status']}, pnl={rec2['pnl']}")

    # Path 3: Time Stop Hit (24h+)
    print("[3] Executing Path 3: Time Stop Hit via _paper_check...")
    id3, occ3 = setup_open_trade("BNBUSDT", opened_ms=t0_ms)
    t24_open = t0_ms + 24 * 3600 * 1000
    c3 = make_candle(t24_open, t24_open + 15 * 60 * 1000 - 1, o=3000, h=3010, l=2990, c=3005)
    current_candles = [c3]
    test_bot._paper_check("BNBUSDT", bot.state.data["positions"]["BNBUSDT"])
    assert "BNBUSDT" not in bot.state.data["positions"], "Position should be popped"
    rec3 = bot.state.data["trades"][-1]
    assert rec3["status"] == "CLOSED_TIME_STOP"
    assert rec3["exit_reason"] == "TIME_STOP_HIT"
    print(f"    Path 3 exited: status={rec3['status']}, pnl={rec3['pnl']}")

    # Path 4: Manual Market Close
    print("[4] Executing Path 4: Manual Market Close via close_position_at_market...")
    id4, occ4 = setup_open_trade("ADAUSDT", opened_ms=t0_ms)
    current_price = 3050.0
    res = test_bot.close_position_at_market("ADAUSDT")
    assert res.get("ok") is True, f"Manual close failed: {res}"
    assert "ADAUSDT" not in bot.state.data["positions"], "Position should be popped"
    rec4 = bot.state.data["trades"][-1]
    assert rec4["status"] == "CLOSED_MANUAL_MARKET"
    assert rec4["exit_reason"] == "CLOSED_MANUAL_MARKET"
    print(f"    Path 4 exited: status={rec4['status']}, pnl={rec4['pnl']}")

    # Compare schemas across all 4 exits
    print("[5] Verifying identical schema across all 4 exit paths...")
    records = [rec1, rec2, rec3, rec4]
    base_keys = set(rec1.keys())
    print(f"    Common fields ({len(base_keys)} keys): {sorted(list(base_keys))}")

    for idx, r in enumerate(records, 1):
        assert set(r.keys()) == base_keys, f"Schema mismatch in path {idx}: diff {set(r.keys()) ^ base_keys}"
        assert isinstance(r["exit"], float) and r["exit"] > 0, f"Path {idx}: invalid exit"
        assert isinstance(r["pnl"], float), f"Path {idx}: invalid pnl"
        assert isinstance(r["exit_time"], float) and r["exit_time"] > 0, f"Path {idx}: invalid exit_time"
        assert r["is_paper"] is True, f"Path {idx}: is_paper should be True"

    # State consistency check
    with bot.state.lock:
        assert len(bot.state.data["positions"]) == 0, "Positions map should be empty"
        assert len(bot.state.data["trades"]) == 4, "Should have exactly 4 trades"
        assert bot.state.data["last_account_equity_usdt"] is not None
        assert bot.state.data["last_account_equity_usdt"] > 0
        print(f"    Final flat baseline: {bot.state.data['last_account_equity_usdt']}")

    print("=== FIX 4 TEST PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()