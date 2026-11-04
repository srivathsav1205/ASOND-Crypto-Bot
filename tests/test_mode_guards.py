import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRATCH_DIR = PROJECT_ROOT / "tests"
SCRATCH_DIR.mkdir(exist_ok=True)

ENV_LIVE = SCRATCH_DIR / ".env_live_test"
ENV_PAPER = SCRATCH_DIR / ".env_paper_test"

def main():
    print("=== STARTING TEST: SCRIPT MODE GUARDS (P1.4) ===")
    
    ENV_LIVE.write_text("ENABLE_LIVE_TRADING=true\n", encoding="utf-8")
    ENV_PAPER.write_text("ENABLE_LIVE_TRADING=false\n", encoding="utf-8")

    paper_file = SCRATCH_DIR / "bot_state_signal_only.json"
    live_file = SCRATCH_DIR / "bot_state_allcoins_live.json"
    paper_file.write_text("{}", encoding="utf-8")
    live_file.write_text("{}", encoding="utf-8")

    # 1. record_external_flow: Live mode rejecting paper file via --state-file
    print("[1] Testing record_external_flow: live mode rejecting paper state file...")
    sub_env = {**os.environ, "ENV_FILE": str(ENV_LIVE)}
    sub_env.pop("BOT_STATE_FILE", None)
    res = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "record_external_flow.py"), "--state-file", str(paper_file), "--amount", "10", "--confirm"],
        capture_output=True, text=True, env=sub_env
    )
    assert res.returncode != 0, f"Expected non-zero exit code, got {res.returncode}"
    assert "does not match live mode" in (res.stdout + res.stderr)
    print("    PASSED: record_external_flow refused paper file in live mode.")

    # 2. record_external_flow: Paper mode rejecting live file via --state-file
    print("[2] Testing record_external_flow: paper mode rejecting live state file...")
    sub_env = {**os.environ, "ENV_FILE": str(ENV_PAPER)}
    sub_env.pop("BOT_STATE_FILE", None)
    res = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "record_external_flow.py"), "--state-file", str(live_file), "--amount", "10", "--confirm"],
        capture_output=True, text=True, env=sub_env
    )
    assert res.returncode != 0, f"Expected non-zero exit code, got {res.returncode}"
    assert "does not match paper mode" in (res.stdout + res.stderr)
    print("    PASSED: record_external_flow refused live file in paper mode.")

    # 3. record_external_flow: Paper mode rejecting live file via BOT_STATE_FILE env var
    print("[3] Testing record_external_flow: rejecting live file set via BOT_STATE_FILE in paper mode...")
    sub_env = {**os.environ, "ENV_FILE": str(ENV_PAPER), "BOT_STATE_FILE": str(live_file)}
    res = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "record_external_flow.py"), "--amount", "10", "--confirm"],
        capture_output=True, text=True, env=sub_env
    )
    assert res.returncode != 0, f"Expected non-zero exit code, got {res.returncode}"
    assert "does not match paper mode" in (res.stdout + res.stderr)
    print("    PASSED: record_external_flow refused live file from BOT_STATE_FILE in paper mode.")

    # 4. initialize_allcoin_live_state: Rejecting paper file via BOT_STATE_FILE
    print("[4] Testing initialize_allcoin_live_state: rejecting paper state file...")
    sub_env = {**os.environ, "BOT_STATE_FILE": str(paper_file)}
    res = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "initialize_allcoin_live_state.py")],
        capture_output=True, text=True, env=sub_env
    )
    assert res.returncode != 0, f"Expected non-zero exit code, got {res.returncode}"
    assert "strictly for live mode state files" in (res.stdout + res.stderr)
    print("    PASSED: initialize_allcoin_live_state refused signal_only file.")

    # 5. initialize_allcoin_live_state: Rejecting non-live pattern
    print("[5] Testing initialize_allcoin_live_state: rejecting non-live filename pattern...")
    custom_paper = SCRATCH_DIR / "my_paper_trading.json"
    custom_paper.write_text("{}", encoding="utf-8")
    sub_env = {**os.environ, "BOT_STATE_FILE": str(custom_paper)}
    res = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "initialize_allcoin_live_state.py")],
        capture_output=True, text=True, env=sub_env
    )
    assert res.returncode != 0, f"Expected non-zero exit code, got {res.returncode}"
    assert "does not match expected live pattern" in (res.stdout + res.stderr)
    print("    PASSED: initialize_allcoin_live_state refused non-live filename pattern.")

    # 6. bot.py: Rejecting live state file in paper mode (P0.1)
    print("[6] Testing bot.py: rejecting live state file in paper mode...")
    sub_env = {
        **os.environ,
        "ENABLE_LIVE_TRADING": "false",
        "BOT_STATE_FILE": str(live_file),
        "BOT_LOG_FILE": str(SCRATCH_DIR / "test_mode_bot.log"),
    }
    cmd_import = f"import sys; sys.path.insert(0, {str(PROJECT_ROOT)!r}); import bot"
    res = subprocess.run(
        [sys.executable, "-c", cmd_import],
        capture_output=True, text=True, env=sub_env
    )
    assert res.returncode != 0, f"Expected bot import to fail on live file in paper mode, got {res.returncode}\n{res.stderr}"
    assert "cannot be a live trading state file" in (res.stdout + res.stderr)
    print("    PASSED: bot.py refused to import with live state file in paper mode.")

    # 7. bot.py: Rejecting paper state file in live mode (P0.1)
    print("[7] Testing bot.py: rejecting paper state file in live mode...")
    sub_env = {
        **os.environ,
        "ENABLE_LIVE_TRADING": "true",
        "BINANCE_BASE_URL": "https://api.binance.com",
        "BOT_STATE_FILE": str(paper_file),
        "BOT_LOG_FILE": str(SCRATCH_DIR / "test_mode_bot.log"),
    }
    res = subprocess.run(
        [sys.executable, "-c", cmd_import],
        capture_output=True, text=True, env=sub_env
    )
    assert res.returncode != 0, f"Expected bot import to fail on paper file in live mode, got {res.returncode}\n{res.stderr}"
    assert "must match '*live*.json'" in (res.stdout + res.stderr)
    print("    PASSED: bot.py refused to import with paper state file in live mode.")

    # Clean up scratch artifacts
    for p in [ENV_LIVE, ENV_PAPER, paper_file, live_file, custom_paper]:
        if p.exists():
            p.unlink()

    print("=== ALL MODE GUARD TESTS PASSED (P0.1 & P1.4 VERIFIED) ===")

if __name__ == "__main__":
    main()
