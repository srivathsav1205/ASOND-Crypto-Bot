import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

SCRATCH_DIR = Path("tests")
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_fix5_state.json"
LOG_PATH = SCRATCH_DIR / "test_fix5.log"

if STATE_PATH.exists():
    STATE_PATH.unlink()
if LOG_PATH.exists():
    LOG_PATH.unlink()

os.environ["BOT_STATE_FILE"] = str(STATE_PATH.resolve())
os.environ["BOT_LOG_FILE"] = str(LOG_PATH.resolve())
os.environ["ENABLE_LIVE_TRADING"] = "false"

import bot

def main():
    print("=== STARTING FIX 5 TEST: BASELINE & FLOW ADJUSTED WIN/DEPOSIT GUARDS ===")

    test_bot = bot.SwingBot.__new__(bot.SwingBot)
    test_bot.last_hour_close = {}
    class MockClient:
        filters = {}
        def price(self, symbol):
            return 3000.0
    test_bot.client = MockClient()

    # Initial clean state
    with bot.state.lock:
        bot.state.data["positions"] = {}
        bot.state.data["trades"] = []
        bot.state.data["manual_pause"] = False
        bot.state.data["manual_pause_reason"] = None
        bot.state.data["account_change_requires_review"] = None
        bot.state.data["net_external_flows_usdt"] = 0.0
        bot.state.data["last_account_equity_usdt"] = 1000.0
        bot.state.save()

    # Part (a): Paper win > $1 does NOT lock the bot
    print("[1] Testing Part (a): Paper win > $1 ($10 win) does not lock...")
    symbol = "ETHUSDT"
    trade_id = f"paper_{symbol}_{int(time.time() * 1000)}"

    # Setup open position and trade
    with bot.state.lock:
        bot.state.data["positions"][symbol] = {
            "trade_id": trade_id,
            "symbol": symbol,
            "quantity": "0.1",
            "entry_price": 3000.0,
            "stop_price": 2900.0,
            "target_price": 3100.0,
            "opened_at": time.time(),
            "is_paper": True,
        }
        bot.state.data["trades"].append({
            "id": trade_id,
            "time": time.time(),
            "symbol": symbol,
            "status": "OPEN",
            "entry": 3000.0,
            "stop": 2900.0,
            "target": 3100.0,
            "qty": "0.1",
            "is_paper": True,
        })
        bot.state.save()

    # Settle with a $10 profit (exit at 3101.5 approx to give ~+$10 net pnl)
    # net_pnl = qty * (exit * (1 - fee) - entry * (1 + fee))
    # 0.1 * (3105 * 0.999 - 3000 * 1.001) ~ 0.1 * (3101.895 - 3003) ~ 0.1 * 98.895 ~ 9.89 USDT > $1
    settled = test_bot._settle_paper_position(symbol, "CLOSED_TARGET", 3105.0, exit_reason="TARGET_HIT")
    assert settled is not None, "Settlement failed"
    pnl = settled["pnl"]
    print(f"    Settled paper win with PnL: +{pnl:.4f} USDT (> $1)")
    assert pnl > 1.0, f"PnL must be > $1, got {pnl}"

    # Now verify baseline after settlement
    with bot.state.lock:
        baseline = bot.state.data.get("last_account_equity_usdt")
        assert baseline is not None, "Baseline should not be None"
        print(f"    Baseline after paper exit: {baseline}")

    # Run check_unrecorded_flat_account_change with fresh equity
    current_equity = test_bot.equity_usdt()
    print(f"    Current equity: {current_equity}")
    ok = test_bot.check_unrecorded_flat_account_change(current_equity)

    assert ok is True, "check_unrecorded_flat_account_change failed after a paper win!"
    assert bot.state.data.get("manual_pause") is False, "Bot was falsely paused after a paper win!"
    assert bot.state.data.get("account_change_requires_review") is None
    print("    Part (a) PASSED: Paper win > $1 did NOT lock the bot.")

    # Part (b): Recorded flow via record_external_flow.py does NOT lock
    print("[2] Testing Part (b): External deposit recorded via record_external_flow.py does not lock...")
    # Reload bot.state to match disk
    with bot.state.lock:
        bot.state.data = json.loads(bot.DATA_FILE.read_text(encoding="utf-8"))

    python_exe = sys.executable
    script_path = str(Path("record_external_flow.py").resolve())
    cmd = [
        python_exe, script_path,
        "--amount", "50.0",
        "--reason", "bank deposit test",
        "--state-file", str(STATE_PATH.resolve()),
        "--confirm"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"record_external_flow.py failed: {res.stderr}\n{res.stdout}"
    print("    record_external_flow.py executed successfully (+50.0 USDT).")

    # Reload state in bot
    with bot.state.lock:
        bot.state.data = json.loads(bot.DATA_FILE.read_text(encoding="utf-8"))
    with bot.state.lock:
        flows = bot.state.data.get("net_external_flows_usdt")
        assert flows == 50.0, f"Expected 50.0 flows, got {flows}"

    # Check equity and unrecorded change check
    current_equity_after_flow = test_bot.equity_usdt()
    print(f"    Current equity after flow: {current_equity_after_flow}")
    ok_flow = test_bot.check_unrecorded_flat_account_change(current_equity_after_flow)

    assert ok_flow is True, "Bot falsely locked after recorded deposit flow!"
    assert bot.state.data.get("manual_pause") is False, "Bot was paused after recorded deposit!"
    assert bot.state.data.get("account_change_requires_review") is None
    print("    Part (b) PASSED: Recorded flow did NOT lock the bot.")

    # Part (c): Unrecorded $5 change STILL locks
    print("[3] Testing Part (c): Unrecorded $5 change still locks...")
    # Simulate unrecorded change where equity jumps by $5 without being in flow ledger
    unrecorded_equity = current_equity_after_flow + 5.0
    print(f"    Simulating unrecorded equity jump: {unrecorded_equity} (+$5 unrecorded)")
    ok_unrecorded = test_bot.check_unrecorded_flat_account_change(unrecorded_equity)

    assert ok_unrecorded is False, "check_unrecorded_flat_account_change should have returned False for unrecorded $5 change!"
    assert bot.state.data.get("manual_pause") is True, "manual_pause should be True"
    assert bot.state.data.get("manual_pause_reason") == "unrecorded_account_change"
    review = bot.state.data.get("account_change_requires_review")
    assert review is not None, "account_change_requires_review should be populated"
    print(f"    Part (c) PASSED: Unrecorded change correctly locked the bot (delta: {review.get('delta_usdt'):+.4f} USDT).")

    print("=== FIX 5 TEST PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()
