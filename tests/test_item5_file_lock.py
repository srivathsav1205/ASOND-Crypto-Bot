import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SCRATCH_DIR = PROJECT_ROOT / "tests"
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_item5_live_state.json"
LOG_PATH = SCRATCH_DIR / "test_item5.log"

if STATE_PATH.exists():
    STATE_PATH.unlink()
if LOG_PATH.exists():
    LOG_PATH.unlink()

os.environ["BOT_STATE_FILE"] = str(STATE_PATH.resolve())
os.environ["BOT_LOG_FILE"] = str(LOG_PATH.resolve())
os.environ["ENABLE_LIVE_TRADING"] = "true"
os.environ["BINANCE_BASE_URL"] = "https://api.binance.com"
os.environ["BINANCE_API_KEY"] = "mock_api_key_for_test"
os.environ["BINANCE_API_SECRET"] = "mock_api_secret_for_test"

import bot

def main():
    print("=== STARTING ITEM 5 TEST: WINDOWS FILE-LOCK & RENAME FAILURE HANDLING ===")

    # Initialize clean state file
    initial_clean = {
        "positions": {"ETHUSDT": {"symbol": "ETHUSDT", "quantity": "0.01", "entry_price": 3000.0}},
        "trades": [],
        "events": [],
        "manual_pause": False,
        "manual_pause_reason": None,
        "recovery_required": {
            "symbol": "ETHUSDT",
            "quantity": "0.01",
            "reason": "Prior protective lock"
        },
        "last_account_equity_usdt": 1000.0,
        "net_external_flows_usdt": 0.0,
        "external_flow_ledger": []
    }
    STATE_PATH.write_text(json.dumps(initial_clean, indent=2), encoding="utf-8")
    bot.DATA_FILE = STATE_PATH
    bot.state = bot.State()

    # --- Part 1: Verify Dashboard Payload does NOT read from disk ---
    print("[1] Verifying dashboard payload reads from memory without disk IO...")
    with bot.state.lock:
        bot.state.data["positions"]["SOLUSDT"] = {"symbol": "SOLUSDT", "quantity": "1.0", "entry_price": 150.0}
    # Delete state file from disk temporarily
    STATE_PATH.unlink()
    assert not STATE_PATH.exists()
    
    # Generate dashboard payload: should succeed reading from in-memory state
    payload = bot._dashboard_payload()
    assert payload is not None
    syms = [p["symbol"] for p in payload["positions"]]
    assert "SOLUSDT" in syms, f"Expected SOLUSDT in in-memory positions, got {syms}"
    assert "ETHUSDT" in syms
    print("    PASSED: Dashboard payload generated cleanly from in-memory state with file missing on disk.")

    # Restore state file on disk
    STATE_PATH.write_text(json.dumps(initial_clean, indent=2), encoding="utf-8")
    bot.state._last_saved_payload = None

    # --- Part 2: Simulate transient Windows file lock (retry succeeds) ---
    print("[2] Simulating transient Windows file-lock (replace fails 3 times, succeeds on attempt 4)...")
    orig_replace = Path.replace
    replace_attempts = 0

    def mock_replace_transient(self, target):
        nonlocal replace_attempts
        replace_attempts += 1
        if replace_attempts <= 3:
            raise OSError(13, f"WinError 5: Access is denied (simulated lock attempt {replace_attempts})")
        return orig_replace(self, target)

    try:
        Path.replace = mock_replace_transient
        with bot.state.lock:
            bot.state.data["events"].append({"time": time.time(), "message": "retry test"})
            ok = bot.state.save()
        assert ok is True, "State save should succeed after transient retries"
        assert replace_attempts == 4, f"Expected 4 attempts, got {replace_attempts}"
        print(f"    PASSED: Save recovered after {replace_attempts} retries without error.")
    finally:
        Path.replace = orig_replace

    # --- Part 3: Simulate permanent rename lock + direct write failure (halts cleanly) ---
    print("[3] Simulating permanent rename lock and direct write failure...")
    bot.state._last_saved_payload = None

    def mock_replace_fail(self, target):
        raise OSError(13, "WinError 5: Access is denied (simulated permanent lock)")

    orig_write_text = Path.write_text
    def mock_write_text_fail(self, data, encoding="utf-8"):
        if self == bot.DATA_FILE:
            raise PermissionError("WinError 5: Access is denied to DATA_FILE")
        return orig_write_text(self, data, encoding=encoding)

    try:
        Path.replace = mock_replace_fail
        Path.write_text = mock_write_text_fail
        with bot.state.lock:
            bot.state.data["events"].append({"time": time.time(), "message": "permanent failure test"})
            # Must NOT raise exception through loop
            ok = bot.state.save()
        
        assert ok is False, "Save should return False on permanent disk failure"
        with bot.state.lock:
            assert bot.state.data["manual_pause"] is True, "Bot must halt entries (manual_pause=True)"
            assert bot.state.data["manual_pause_reason"] == "state_save_disk_failure"
            # Crucial: recovery_required must remain intact!
            assert bot.state.data.get("recovery_required") is not None
            assert bot.state.data["recovery_required"]["symbol"] == "ETHUSDT"
            assert bot.state.data["recovery_required"]["reason"] == "Prior protective lock"
        print("    PASSED: Permanent save failure halted entries cleanly, set manual_pause, and kept recovery lock intact.")
    finally:
        Path.replace = orig_replace
        Path.write_text = orig_write_text

    print("=== ITEM 5 TEST PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()
