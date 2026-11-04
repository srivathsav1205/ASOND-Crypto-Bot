import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

SCRATCH_DIR = Path("tests")
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_fix1_state.json"
LOG_PATH = SCRATCH_DIR / "test_fix1.log"

if STATE_PATH.exists():
    STATE_PATH.unlink()
if LOG_PATH.exists():
    LOG_PATH.unlink()

os.environ["BOT_STATE_FILE"] = str(STATE_PATH.resolve())
os.environ["BOT_LOG_FILE"] = str(LOG_PATH.resolve())
os.environ["ENABLE_LIVE_TRADING"] = "false"
# Keep API keys present in env, reproducing the 12:00 and 12:20 production environment
os.environ["BINANCE_API_KEY"] = "prod_like_api_key_test"
os.environ["BINANCE_API_SECRET"] = "prod_like_api_secret_test"

import bot

def main():
    print("=== STARTING FIX 1 TEST: 12:00/12:20 REAL FAILURE REPRODUCTION & NAN GUARDS ===")

    test_bot = bot.SwingBot.__new__(bot.SwingBot)
    test_bot.last_hour_close = {}

    class MockClient:
        filters = {}
        def price(self, symbol):
            # If price check encounters NaN in unquoted or illiquid pair
            return float("nan")
        def request(self, method, endpoint, *args, **kwargs):
            if endpoint == "/api/v3/account":
                raise AssertionError("Live /api/v3/account was called in paper mode! Root cause of 12:00 network crash.")
            return {}

    test_bot.client = MockClient()

    # Part A: Exact 12:00/12:20 log reproduction
    # Log: "BTCUSDT PAPER TARGET HIT: +1.8780 USDT" followed by "could not read equity after exit; entries locked: invalid equity nan"
    print("[1] Reproducing exact 12:00/12:20 log sequence: +1.8780 USDT paper exit with API keys in env...")
    with bot.state.lock:
        bot.state.data["positions"] = {}
        bot.state.data["trades"] = [
            {
                "symbol": "BTCUSDT",
                "status": "CLOSED_TARGET",
                "pnl": 1.8780,
                "gross_realized_pnl_usdt_estimate": 1.8780,
                "is_paper": True,
            }
        ]
        bot.state.data["net_external_flows_usdt"] = 0.0
        bot.state.data["last_account_equity_usdt"] = 1000.0
        bot.state.data["manual_pause"] = False
        bot.state.data["manual_pause_reason"] = None
        bot.state.data["account_change_requires_review"] = None
        bot.state.save()

    bals = test_bot.balances()
    assert "USDT" in bals, "USDT balance missing from paper balances"
    assert bals["USDT"][0] == 1001.878, f"Expected 1001.878 cash, got {bals['USDT'][0]}"
    print(f"    Paper balances evaluated without live account call: {bals}")

    eq_1200 = test_bot.equity_usdt(bals)
    assert isinstance(eq_1200, float) and math.isfinite(eq_1200), f"Equity must be finite float, got {eq_1200}"
    assert eq_1200 == 1001.878, f"Expected 1001.878, got {eq_1200}"
    print(f"    Evaluated paper equity: {eq_1200} (finite, finite guard passed)")

    # Rebaseline
    baseline_ok = test_bot.set_flat_baseline()
    assert baseline_ok is True, "set_flat_baseline failed on 12:00 state"
    with bot.state.lock:
        base_val = bot.state.data.get("last_account_equity_usdt")
        assert base_val == 1001.878, f"Expected baseline 1001.878, got {base_val}"
        assert bot.state.data.get("manual_pause") is False, "Bot was falsely paused"
        assert bot.state.data.get("manual_pause_reason") is None
    print(f"    Set flat baseline: {base_val} (zero false locks, no 'invalid equity nan')")

    flat_check = test_bot.check_unrecorded_flat_account_change(eq_1200)
    assert flat_check is True, "Flat account check failed after 12:00 exit"
    print("    Part A PASSED: Real failure scenario completely resolved.")

    # Part B: Extreme corrupted state with NaN / None / string 'nan' values
    print("[2] Testing corrupted state with NaN, None, string 'nan' values in every field...")
    with bot.state.lock:
        bot.state.data["positions"] = {
            "ETHUSDT": {
                "symbol": "ETHUSDT",
                "quantity": "nan",
                "entry_price": None,
                "is_paper": True,
            },
            "BTCUSDT": {
                "symbol": "BTCUSDT",
                "quantity": 0.005,
                "entry_price": 60000.0,
                "is_paper": True,
            }
        }
        bot.state.data["trades"] = [
            {"status": "CLOSED_TARGET", "pnl": float("nan"), "is_paper": True},
            {"status": "CLOSED_STOP", "pnl": "nan", "is_paper": True},
            {"status": "CLOSED_TARGET", "pnl": 12.50, "is_paper": True},
            {"status": "CLOSED_MANUAL_MARKET", "pnl": None, "gross_realized_pnl_usdt_estimate": "nan", "is_paper": True},
        ]
        bot.state.data["net_external_flows_usdt"] = float("nan")
        bot.state.data["last_account_equity_usdt"] = float("nan")
        bot.state.data["manual_pause"] = False
        bot.state.data["manual_pause_reason"] = None
        bot.state.save()

    eq_corrupt = test_bot.equity_usdt()
    assert isinstance(eq_corrupt, float) and math.isfinite(eq_corrupt), f"Equity must be finite float, got {eq_corrupt}"
    assert eq_corrupt > 0, f"Equity must be positive, got {eq_corrupt}"
    # Initial 1000 + 12.50 valid trade = 1012.50
    assert eq_corrupt == 1012.50, f"Expected 1012.50, got {eq_corrupt}"
    print(f"    Evaluated paper equity under extreme corruption: {eq_corrupt} (safely sanitized all NaNs)")

    with bot.state.lock:
        bot.state.data["positions"] = {}
        bot.state.save()

    ok_corrupt = test_bot.set_flat_baseline()
    assert ok_corrupt is True, "set_flat_baseline failed under corrupted flows"
    with bot.state.lock:
        baseline_corrupt = bot.state.data.get("last_account_equity_usdt")
        assert baseline_corrupt == 1012.50, f"Expected baseline 1012.50, got {baseline_corrupt}"
        assert bot.state.data.get("manual_pause") is False
    print(f"    Set flat baseline under corrupted flows: {baseline_corrupt} (manual_pause=False)")

    print("=== FIX 1 TEST PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()
