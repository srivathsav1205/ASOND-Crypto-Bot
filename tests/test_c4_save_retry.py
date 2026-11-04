import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SCRATCH_DIR = PROJECT_ROOT / "tests"
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_c4_live_state.json"
LOG_PATH = SCRATCH_DIR / "test_c4.log"

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
    print("=== STARTING C4 TEST: STATE SAVE RETRY AND FAIL-CLOSED PRESERVATION ===")

    initial_clean = {
        "positions": {"ETHUSDT": {"symbol": "ETHUSDT", "quantity": "0.01", "entry_price": 3000.0}},
        "trades": [],
        "events": [],
        "manual_pause": False,
        "manual_pause_reason": None,
        "recovery_required": None,
        "last_account_equity_usdt": 1000.0,
        "net_external_flows_usdt": 0.0,
        "external_flow_ledger": []
    }
    STATE_PATH.write_text(json.dumps(initial_clean, indent=2), encoding="utf-8")
    bot.DATA_FILE = STATE_PATH
    bot.state = bot.State()

    # Test 1: Corrupted non-finite state is refused, file preserved intact
    print("[1] Testing corrupted state refusal (C4 fail-closed)...")
    with bot.state.lock:
        bot.state.data["last_account_equity_usdt"] = float("nan")
        ok = bot.state.save()
    assert ok is False, "Save of corrupted state must return False"
    # Verify file on disk was preserved with original non-corrupted state
    disk_data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    assert disk_data["last_account_equity_usdt"] == 1000.0, "Disk file was corrupted despite save refusal!"
    print("    PASSED: Corrupted save was refused and previous disk state preserved.")

    # Reset in-memory state
    with bot.state.lock:
        bot.state.data["last_account_equity_usdt"] = 1000.0

    # Test 2: Transient file replace failure (retries and succeeds)
    print("[2] Testing transient replace failure retry recovery...")
    orig_replace = Path.replace
    attempts = 0
    def mock_replace(self, target):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise OSError(13, "WinError 5: Access is denied (transient lock)")
        return orig_replace(self, target)

    try:
        Path.replace = mock_replace
        with bot.state.lock:
            bot.state.data["events"].append({"time": time.time(), "message": "retry event"})
            ok = bot.state.save()
        assert ok is True, f"Save should succeed after transient retries (attempts={attempts})"
        assert attempts == 3
        print(f"    PASSED: Save recovered after {attempts} attempts cleanly.")
    finally:
        Path.replace = orig_replace

    # Test 3: Complete disk failure (replace + direct write fail) -> halts cleanly
    print("[3] Testing complete disk failure fail-closed behavior...")
    bot.state._last_saved_payload = None

    def mock_replace_perm(self, target):
        raise OSError(13, "Permanent replace failure")

    orig_write = Path.write_text
    def mock_write_perm(self, data, encoding="utf-8"):
        if self == bot.DATA_FILE:
            raise PermissionError("Permanent write failure")
        return orig_write(self, data, encoding=encoding)

    try:
        Path.replace = mock_replace_perm
        Path.write_text = mock_write_perm
        with bot.state.lock:
            bot.state.data["events"].append({"time": time.time(), "message": "permanent fail event"})
            ok = bot.state.save()
        assert ok is False
        with bot.state.lock:
            assert bot.state.data["manual_pause"] is True
            assert bot.state.data["manual_pause_reason"] == "state_save_disk_failure"
        print("    PASSED: Complete disk failure halted entries safely without raising exception.")
    finally:
        Path.replace = orig_replace
        Path.write_text = orig_write

    print("=== C4 TEST PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()
