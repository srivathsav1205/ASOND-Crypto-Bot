import json
import os
import sys
from pathlib import Path

# Always relative to this script's directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SCRATCH_DIR = PROJECT_ROOT / "tests"
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_item1_state.json"
LOG_PATH = SCRATCH_DIR / "test_item1.log"

if STATE_PATH.exists():
    STATE_PATH.unlink()
if LOG_PATH.exists():
    LOG_PATH.unlink()

os.environ["BOT_STATE_FILE"] = str(STATE_PATH.resolve())
os.environ["BOT_LOG_FILE"] = str(LOG_PATH.resolve())
os.environ["ENABLE_LIVE_TRADING"] = "false"

import bot

def main():
    print("=== STARTING ITEM 1 TEST: REFUSE SAVING NAN VALUES TO DISK ===")

    # 1. Establish a valid initial state on disk
    initial_clean_state = {
        "positions": {},
        "trades": [{"id": "t1", "symbol": "BTCUSDT", "status": "CLOSED", "pnl": 5.0}],
        "net_external_flows_usdt": 0.0,
        "last_account_equity_usdt": 1005.0,
        "manual_pause": False,
    }
    with bot.state.lock:
        bot.state.data = dict(initial_clean_state)
        saved = bot.state.save()
        assert saved is True or saved is None, "Initial clean state save failed"

    initial_content = STATE_PATH.read_text(encoding="utf-8")
    assert "NaN" not in initial_content, "Initial state has NaN"
    print("[1] Initial clean state saved successfully on disk.")

    # 2. Corrupt state with float('nan') in trade PnL (matching test_fix1_state.json)
    print("[2] Testing state with float('nan') in trade PnL...")
    with bot.state.lock:
        bot.state.data["trades"].append({
            "id": "t2", "symbol": "ETHUSDT", "status": "CLOSED_TARGET", "pnl": float("nan")
        })
        save_res = bot.state.save()

    assert save_res is False, f"Expected save() to return False when float('nan') in trade PnL, got {save_res}"
    content_after_nan_pnl = STATE_PATH.read_text(encoding="utf-8")
    assert "NaN" not in content_after_nan_pnl, "Disk file was corrupted with literal NaN!"
    assert content_after_nan_pnl == initial_content, "Disk file was modified despite refused save!"
    print("    VERIFIED: Save refused on float('nan') PnL; disk file intact with zero NaNs.")

    # 3. Corrupt state with string "nan" in net_external_flows_usdt
    print("[3] Testing state with string 'nan' in net_external_flows_usdt...")
    with bot.state.lock:
        bot.state.data["trades"].pop()  # remove bad trade
        bot.state.data["net_external_flows_usdt"] = "nan"
        save_res2 = bot.state.save()

    assert save_res2 is False, f"Expected save() to return False when 'nan' in flows, got {save_res2}"
    content_after_str_nan = STATE_PATH.read_text(encoding="utf-8")
    assert "NaN" not in content_after_str_nan
    assert content_after_str_nan == initial_content
    print("    VERIFIED: Save refused on string 'nan' in flows; disk file intact.")

    # 4. Corrupt state with float('inf') in baseline
    print("[4] Testing state with float('inf') in baseline...")
    with bot.state.lock:
        bot.state.data["net_external_flows_usdt"] = 0.0
        bot.state.data["last_account_equity_usdt"] = float("inf")
        save_res3 = bot.state.save()

    assert save_res3 is False, f"Expected save() to return False on float('inf') baseline, got {save_res3}"
    content_after_inf = STATE_PATH.read_text(encoding="utf-8")
    assert "Infinity" not in content_after_inf
    assert content_after_inf == initial_content
    print("    VERIFIED: Save refused on float('inf') baseline; disk file intact.")

    # 5. Clean up state and verify clean save works
    print("[5] Testing clean state save...")
    with bot.state.lock:
        bot.state.data["last_account_equity_usdt"] = 1010.0
        bot.state.data["trades"].append({
            "id": "t3", "symbol": "SOLUSDT", "status": "CLOSED", "pnl": 5.0
        })
        save_clean = bot.state.save()

    assert save_clean is True, "Clean save should return True"
    final_content = STATE_PATH.read_text(encoding="utf-8")
    loaded = json.loads(final_content)
    assert loaded["last_account_equity_usdt"] == 1010.0
    assert len(loaded["trades"]) == 2
    assert "NaN" not in final_content
    print("    VERIFIED: Clean save succeeded and disk file contains valid JSON with 0 NaNs.")

    print("=== ITEM 1 TEST PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()
