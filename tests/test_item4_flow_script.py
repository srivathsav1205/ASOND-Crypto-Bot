import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SCRATCH_DIR = PROJECT_ROOT / "tests"
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_item4_state.json"
ENV_PATH = SCRATCH_DIR / ".env"

def main():
    print("=== STARTING ITEM 4 TEST: RECORD_EXTERNAL_FLOW.PY HARDENING ===")

    # --- Part 1: Test .env parsing with spaces and inline comments ---
    print("[1] Testing .env parsing with spaces and inline comments...")
    
    # Case A: Live mode with spaces and inline comment
    ENV_PATH.write_text("  ENABLE_LIVE_TRADING  =   true   # live trading enabled!\nOTHER_VAR=123\n", encoding="utf-8")
    
    import record_external_flow
    # Temporarily monkey-patch ROOT in record_external_flow to test find_state_file
    orig_root = record_external_flow.ROOT
    record_external_flow.ROOT = SCRATCH_DIR
    
    # Ensure no explicit env var
    old_env_state = os.environ.pop("BOT_STATE_FILE", None)
    
    # Save dummy state files in scratch dir
    live_state_file = SCRATCH_DIR / "bot_state_allcoins_live.json"
    paper_state_file = SCRATCH_DIR / "bot_state_signal_only.json"
    live_state_file.write_text("{}", encoding="utf-8")
    paper_state_file.write_text("{}", encoding="utf-8")

    resolved_file = record_external_flow.find_state_file()
    assert resolved_file.name == "bot_state_allcoins_live.json", f"Expected live file, got {resolved_file.name}"
    print("    PASSED: 'ENABLE_LIVE_TRADING  =   true   # live' resolved to live state file.")

    # Case B: Paper mode with spaces and inline comment
    ENV_PATH.write_text("ENABLE_LIVE_TRADING=false # paper mode only\n", encoding="utf-8")
    resolved_file = record_external_flow.find_state_file()
    assert resolved_file.name == "bot_state_signal_only.json", f"Expected paper file, got {resolved_file.name}"
    print("    PASSED: 'ENABLE_LIVE_TRADING=false # paper' resolved to paper state file.")

    # Case C: Undetermined mode (missing ENABLE_LIVE_TRADING)
    ENV_PATH.write_text("SOME_OTHER_KEY=123\n# ENABLE_LIVE_TRADING=true (commented out)\n", encoding="utf-8")
    sub_env = {**os.environ, "ENV_FILE": str(ENV_PATH)}
    sub_env.pop("BOT_STATE_FILE", None)
    res = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "record_external_flow.py"), "--amount", "50", "--confirm"],
        cwd=str(SCRATCH_DIR),
        capture_output=True,
        text=True,
        env=sub_env
    )
    assert res.returncode != 0, f"Expected non-zero exit when mode is undetermined, got {res.returncode}"
    assert "undetermined" in (res.stdout + res.stderr).lower()
    print("    PASSED: Undetermined mode correctly refused with non-zero exit code (no silent fallback).")

    # --- Part 2: Never clear recovery_required ---
    print("[2] Testing lock protection: record_external_flow must NEVER clear recovery_required...")
    ENV_PATH.write_text("ENABLE_LIVE_TRADING=false\n", encoding="utf-8")
    initial_state = {
        "positions": {"ETHUSDT": {"symbol": "ETHUSDT"}},
        "trades": [],
        "manual_pause": True,
        "manual_pause_reason": "oco_failed",
        "recovery_required": {
            "symbol": "ETHUSDT",
            "quantity": "0.01",
            "reason": "Market sell failed; recovery required"
        },
        "account_change_requires_review": True,
        "net_external_flows_usdt": 0.0,
        "external_flow_ledger": []
    }
    STATE_PATH.write_text(json.dumps(initial_state, indent=2), encoding="utf-8")

    # Run record_external_flow on this state file
    sub_env = {**os.environ, "ENV_FILE": str(ENV_PATH)}
    sub_env.pop("BOT_STATE_FILE", None)
    res = subprocess.run(
        [
            sys.executable, str(PROJECT_ROOT / "record_external_flow.py"),
            "--state-file", str(STATE_PATH),
            "--amount", "100.0",
            "--reason", "bank deposit",
            "--confirm"
        ],
        capture_output=True,
        text=True,
        env=sub_env
    )
    assert res.returncode == 0, f"Script failed: {res.stderr}\n{res.stdout}"
    
    saved_data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    assert saved_data.get("recovery_required") is not None, "CRITICAL ERROR: recovery_required was cleared by flow script!"
    assert saved_data["recovery_required"]["symbol"] == "ETHUSDT"
    # manual_pause should STILL be True because manual_pause_reason was "oco_failed", NOT "unrecorded_account_change"
    assert saved_data["manual_pause"] is True
    assert saved_data["manual_pause_reason"] == "oco_failed"
    # account_change_requires_review was cleared as expected
    assert saved_data["account_change_requires_review"] is False
    print("    PASSED: recovery_required remained 100% intact and unmolested.")

    # --- Part 3: Test clearing unrecorded_account_change lock ---
    print("[3] Testing unrecorded_account_change lock clearance...")
    state_unrec = {
        "positions": {},
        "trades": [],
        "manual_pause": True,
        "manual_pause_reason": "unrecorded_account_change",
        "recovery_required": None,
        "account_change_requires_review": True,
        "net_external_flows_usdt": 100.0,
        "external_flow_ledger": []
    }
    STATE_PATH.write_text(json.dumps(state_unrec, indent=2), encoding="utf-8")

    res = subprocess.run(
        [
            sys.executable, str(PROJECT_ROOT / "record_external_flow.py"),
            "--state-file", str(STATE_PATH),
            "--amount", "50.0",
            "--reason", "second deposit",
            "--confirm"
        ],
        capture_output=True,
        text=True,
        env=sub_env
    )
    assert res.returncode == 0, f"Script failed: {res.stderr}"
    saved_data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    assert saved_data["manual_pause"] is False
    assert saved_data["manual_pause_reason"] is None
    assert saved_data["account_change_requires_review"] is False
    assert saved_data["net_external_flows_usdt"] == 150.0
    print("    PASSED: unrecorded_account_change lock cleared cleanly.")

    # --- Part 4: Test single key external_flow_ledger and migration of legacy external_flows ---
    print("[4] Testing external_flow_ledger migration and single key constraint...")
    state_legacy = {
        "positions": {},
        "trades": [],
        "net_external_flows_usdt": 20.0,
        "external_flows": [
            {"time": 1000.0, "amount": 20.0, "reason": "legacy flow", "net_flows_after": 20.0}
        ]
    }
    STATE_PATH.write_text(json.dumps(state_legacy, indent=2), encoding="utf-8")

    res = subprocess.run(
        [
            sys.executable, str(PROJECT_ROOT / "record_external_flow.py"),
            "--state-file", str(STATE_PATH),
            "--amount", "30.0",
            "--reason", "new flow",
            "--confirm"
        ],
        capture_output=True,
        text=True,
        env=sub_env
    )
    assert res.returncode == 0, f"Script failed: {res.stderr}"
    saved_data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    assert "external_flows" not in saved_data, "Legacy 'external_flows' key must be removed"
    assert "external_flow_ledger" in saved_data, "Must use 'external_flow_ledger'"
    assert len(saved_data["external_flow_ledger"]) == 2, f"Expected 2 ledger items, got {len(saved_data['external_flow_ledger'])}"
    assert saved_data["external_flow_ledger"][0]["reason"] == "legacy flow"
    assert saved_data["external_flow_ledger"][1]["reason"] == "new flow"
    assert saved_data["net_external_flows_usdt"] == 50.0
    print("    PASSED: Legacy external_flows migrated to external_flow_ledger cleanly.")

    # --- Part 5: Bot Integration Test (flow remains after bot save) ---
    print("[5] Testing bot integration: record flow, verify bot retains flow after save...")
    os.environ["BOT_STATE_FILE"] = str(STATE_PATH)
    os.environ["BOT_LOG_FILE"] = str(SCRATCH_DIR / "test_item4_bot.log")
    
    # Reload bot state with our STATE_PATH
    import bot
    bot.DATA_FILE = STATE_PATH
    bot.state = bot.State()
    
    assert bot.state.data["net_external_flows_usdt"] == 50.0
    assert len(bot.state.data["external_flow_ledger"]) == 2

    # Bot performs a save
    with bot.state.lock:
        bot.state.data["events"].append({"time": time.time(), "message": "Bot loop heartbeat"})
        bot.state.save()

    # Verify disk
    data_after_bot = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    assert data_after_bot["net_external_flows_usdt"] == 50.0
    assert len(data_after_bot["external_flow_ledger"]) == 2
    print("    PASSED: External flows fully retained across bot state loads and saves.")

    # Clean up scratch files
    for p in [live_state_file, paper_state_file, ENV_PATH]:
        if p.exists():
            p.unlink()

    print("=== ITEM 4 TEST PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()
