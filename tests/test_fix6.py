import os
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

SCRATCH_DIR = Path("tests")
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_fix6_live_state.json"
LOG_PATH = SCRATCH_DIR / "test_fix6.log"

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
    print("=== STARTING FIX 6 TEST: LIVE OCO MAKER GUARD WITH NO OCO ===")

    test_bot = bot.SwingBot.__new__(bot.SwingBot)
    test_bot.last_hour_close = {}

    symbol = "ETHUSDT"
    target_price = 3120.0
    stop_price = 2940.0
    entry_price = 3000.0

    placed_orders = []

    class MockLiveClient:
        filters = {
            "ETHUSDT": {
                "LOT_SIZE": {"minQty": "0.001", "stepSize": "0.001"},
                "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"minNotional": "10.0"},
            }
        }
        price_calls = 0
        def price(self, sym):
            self.price_calls += 1
            if self.price_calls == 1:
                return 3000.0  # Fresh price before BUY
            return 3125.0     # Price immediately after BUY fill (OCO maker guard)

        wallet_usdt = 1000.0

        def account(self):
            return {
                "balances": [
                    {"asset": "USDT", "free": str(self.wallet_usdt), "locked": "0.0"},
                    {"asset": "ETH", "free": "0.0", "locked": "0.0"},
                    {"asset": "BNB", "free": "0.1", "locked": "0.0"},
                ]
            }

        def order(self, payload):
            placed_orders.append(payload)
            side = payload.get("side")
            qty = payload.get("quantity")
            if side == "BUY":
                quote = float(qty) * entry_price
                self.wallet_usdt -= quote
                return {
                    "orderId": 1001,
                    "symbol": symbol,
                    "status": "FILLED",
                    "executedQty": qty,
                    "cummulativeQuoteQty": str(quote),
                    "side": "BUY"
                }
            elif side == "SELL":
                quote = float(qty) * 3125.0
                self.wallet_usdt += quote
                return {
                    "orderId": 1002,
                    "symbol": symbol,
                    "status": "FILLED",
                    "executedQty": qty,
                    "cummulativeQuoteQty": str(quote),
                    "side": "SELL"
                }
            raise ValueError(f"Unexpected side: {side}")

        def request(self, method, endpoint, params=None, signed=False):
            if endpoint == "/api/v3/myTrades":
                return []
            raise ValueError(f"Unexpected endpoint: {endpoint}")

        def oco_sell(self, *args, **kwargs):
            raise AssertionError("oco_sell was called when price breached target immediately! Fix 6 strictly forbids this.")

    test_bot.client = MockLiveClient()

    # Reset state (DO NOT MOCK set_flat_baseline)
    with bot.state.lock:
        bot.state.data["positions"] = {}
        bot.state.data["trades"] = []
        bot.state.data["manual_pause"] = False
        bot.state.data["manual_pause_reason"] = None
        bot.state.data["recovery_required"] = None
        bot.state.data["last_account_equity_usdt"] = 1000.0
        bot.state.save()

    sig = {
        "symbol": symbol,
        "entry": entry_price,
        "stop": stop_price,
        "target": target_price,
        "level": entry_price,
        "score": 90,
        "score_desc": "Score 90/100",
    }
    equity = 1000.0
    balances = {"USDT": (1000.0, 0.0)}

    print("[1] Calling open_trade where market price reached target immediately after BUY fill...")
    res = test_bot.open_trade(sig, equity, balances)

    print(f"    open_trade returned: {res}")
    assert res is not None and res.get("ok") is True, f"open_trade failed: {res}"

    # Verify orders placed
    assert len(placed_orders) == 2, f"Expected 2 orders (BUY then direct MARKET SELL), got {len(placed_orders)}"
    assert placed_orders[0]["side"] == "BUY"
    assert placed_orders[1]["side"] == "SELL"
    assert placed_orders[1]["type"] == "MARKET"
    print(f"    Exchange orders verified: BUY ({placed_orders[0]['quantity']}) -> MARKET SELL ({placed_orders[1]['quantity']})")

    # Verify state clean
    with bot.state.lock:
        assert symbol not in bot.state.data["positions"], "Position must be cleared"
        assert bot.state.data.get("recovery_required") is None, "recovery_required must be None"
        assert bot.state.data.get("manual_pause") is False, "manual_pause must be False"
        assert len(bot.state.data["trades"]) == 1, "Should have 1 recorded trade"
        trade = bot.state.data["trades"][0]
        assert trade["status"] == "CLOSED_TARGET", f"Expected CLOSED_TARGET, got {trade['status']}"
        assert trade["exit_reason"] == "IMMEDIATE_TARGET_BREACH_BEFORE_OCO"
        assert trade["gross_realized_pnl_usdt_estimate"] > 0
        print(f"    Trade ledger verified: status={trade['status']}, exit={trade['exit']}, pnl_est={trade['gross_realized_pnl_usdt_estimate']}")

        expected_baseline = round(1000.0 + float(trade["gross_realized_pnl_usdt_estimate"]), 4)
        actual_baseline = bot.state.data.get("last_account_equity_usdt")
        assert actual_baseline == expected_baseline, f"Expected baseline {expected_baseline}, got {actual_baseline}"
        print(f"    Unmocked baseline verified: {actual_baseline} == starting_equity + pnl ({expected_baseline})")

    print("=== FIX 6 TEST PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()
