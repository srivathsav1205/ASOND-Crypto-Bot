import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SCRATCH_DIR = PROJECT_ROOT / "tests"
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_item6_live_state.json"

def main():
    print("=== STARTING ITEM 6 TEST: INITIALIZE_ALLCOIN_LIVE_STATE HARDENING ===")

    # --- Part 1a: Test --force refusal when recovery_required is set ---
    print("[1a] Testing --force refusal when recovery_required is active...")
    state_with_recovery = {
        "positions": {},
        "trades": [],
        "recovery_required": {
            "symbol": "ETHUSDT",
            "quantity": "0.01",
            "reason": "Unresolved exchange order error"
        },
        "manual_pause": True,
        "manual_pause_reason": "recovery_required"
    }
    STATE_PATH.write_text(json.dumps(state_with_recovery, indent=2), encoding="utf-8")

    sub_env = {
        **os.environ,
        "BOT_STATE_FILE": str(STATE_PATH),
        "BINANCE_BASE_URL": "https://api.binance.com",
        "BINANCE_API_KEY": "dummy_key",
        "BINANCE_API_SECRET": "dummy_secret",
    }

    res = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "initialize_allcoin_live_state.py"), "--force"],
        capture_output=True,
        text=True,
        env=sub_env
    )
    assert res.returncode != 0, f"Expected non-zero exit code when recovery_required is active, got {res.returncode}"
    assert "recovery_required" in res.stdout or "recovery_required" in res.stderr
    print("    PASSED: --force was refused because recovery_required lock is active.")

    # --- Part 1b: Test --force refusal when state file is unreadable/corrupted JSON (P0.3) ---
    print("[1b] Testing --force refusal when state file has corrupted JSON...")
    STATE_PATH.write_text("{ this is corrupted json state file", encoding="utf-8")
    res = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "initialize_allcoin_live_state.py"), "--force"],
        capture_output=True,
        text=True,
        env=sub_env
    )
    assert res.returncode != 0, f"Expected non-zero exit code on unreadable state, got {res.returncode}"
    assert "could not be parsed as valid JSON" in (res.stdout + res.stderr)
    print("    PASSED: --force was refused on unreadable state file (untracked positions protected).")

    # --- Part 2a: Test untracked asset check (holding BTC > $1.00) ---
    print("[2a] Testing that holding untracked asset BTC > $1.00 is refused...")
    import initialize_allcoin_live_state

    def mock_api_request(method, path, params=None, signed=False):
        if path == "/api/v3/account":
            return {
                "canTrade": True,
                "balances": [
                    {"asset": "USDT", "free": "1000.0", "locked": "0.0"},
                    {"asset": "BNB", "free": "0.1", "locked": "0.0"},
                    {"asset": "BTC", "free": "0.001", "locked": "0.0"},  # 0.001 BTC * $60,000 = $60 > $1.00!
                ]
            }
        if path == "/api/v3/openOrders":
            return []
        if path == "/api/v3/ticker/price":
            sym = (params or {}).get("symbol")
            if sym == "BNBUSDT":
                return {"symbol": "BNBUSDT", "price": "600.0"}
            if sym == "BTCUSDT":
                return {"symbol": "BTCUSDT", "price": "60000.0"}
            return {"price": "1.0"}
        raise ValueError(f"Unexpected path: {path}")

    initialize_allcoin_live_state.api_request = mock_api_request
    initialize_allcoin_live_state.BASE_URL = "https://api.binance.com"
    initialize_allcoin_live_state.API_KEY = "dummy_key"
    initialize_allcoin_live_state.API_SECRET = "dummy_secret"
    initialize_allcoin_live_state.TARGET_FILE = STATE_PATH

    if STATE_PATH.exists():
        STATE_PATH.unlink()

    sys_exit_code = None
    try:
        sys.argv = ["initialize_allcoin_live_state.py", "--force"]
        initialize_allcoin_live_state.main()
    except SystemExit as exc:
        sys_exit_code = exc.code

    assert sys_exit_code == 1, f"Expected sys.exit(1) on untracked BTC holding, got {sys_exit_code}"
    print("    PASSED: BTC holding was valued ($60.00) and initialization refused.")

    # --- Part 2b: Test valuation fail-closed when ticker/price query fails (P0.4) ---
    print("[2b] Testing fail-closed behavior when ticker price lookup fails...")
    def mock_broken_price_api(method, path, params=None, signed=False):
        if path == "/api/v3/account":
            return {
                "canTrade": True,
                "balances": [
                    {"asset": "USDT", "free": "1000.0", "locked": "0.0"},
                    {"asset": "ETH", "free": "0.5", "locked": "0.0"},
                ]
            }
        if path == "/api/v3/openOrders":
            return []
        if path == "/api/v3/ticker/price":
            raise RuntimeError("Binance API 500 Internal Server Error")
        raise ValueError(f"Unexpected path: {path}")

    initialize_allcoin_live_state.api_request = mock_broken_price_api
    sys_exit_code = None
    try:
        sys.argv = ["initialize_allcoin_live_state.py", "--force"]
        initialize_allcoin_live_state.main()
    except SystemExit as exc:
        sys_exit_code = exc.code

    assert sys_exit_code == 1, f"Expected sys.exit(1) when price lookup fails, got {sys_exit_code}"
    print("    PASSED: Price query failure failed closed (did not assume 1 ETH = $1).")

    # --- Part 3: Test clean initialization with only USDT and BNB fee assets (P1.8, P1.9) ---
    print("[3] Testing clean initialization with USDT capital and BNB fee reserve...")
    def mock_clean_account(method, path, params=None, signed=False):
        if path == "/api/v3/account":
            return {
                "canTrade": True,
                "balances": [
                    {"asset": "USDT", "free": "1000.0", "locked": "0.0"},
                    {"asset": "BNB", "free": "0.1", "locked": "0.0"},  # 0.1 BNB * $600 = $60 fee equity
                ]
            }
        if path == "/api/v3/openOrders":
            return []
        if path == "/api/v3/ticker/price":
            return {"symbol": "BNBUSDT", "price": "600.0"}
        raise ValueError(f"Unexpected path: {path}")

    initialize_allcoin_live_state.api_request = mock_clean_account
    sys_exit_code = None
    try:
        sys.argv = ["initialize_allcoin_live_state.py", "--force"]
        initialize_allcoin_live_state.main()
    except SystemExit as exc:
        sys_exit_code = exc.code

    assert sys_exit_code is None or sys_exit_code == 0
    assert STATE_PATH.exists()
    created_state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    
    # Baseline matches bot.py equity formula: strategy USDT only ($1000.00), BNB excluded from baseline
    assert created_state["last_account_equity_usdt"] == 1000.0, f"Expected 1000.0 USDT strategy equity, got {created_state['last_account_equity_usdt']}"
    assert created_state["net_external_flows_usdt"] == 0.0
    assert "external_flow_ledger" in created_state
    assert created_state["external_flow_ledger"] == []
    assert "external_flows" not in created_state
    assert created_state["positions"] == {}
    assert created_state["recovery_required"] is None
    
    # Verify complete schema (P1.9)
    for required_key in ["last_hour_close", "last_15m_close", "setups_15m", "confirmed_15m_patterns"]:
        assert required_key in created_state, f"Missing required schema key: {required_key}"

    print(f"    PASSED: Clean state initialized successfully: strategy equity=${created_state['last_account_equity_usdt']} (matches bot.py formula), complete schema verified.")

    # Clean up scratch state
    if STATE_PATH.exists():
        STATE_PATH.unlink()

    print("=== ITEM 6 TEST PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()
