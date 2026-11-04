import os
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

SCRATCH_DIR = Path("tests")
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_fix8_live_state.json"
LOG_PATH = SCRATCH_DIR / "test_fix8.log"

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
    print("=== STARTING FIX 8 TEST: WATCHDOG CHECKS OCO STATUS FIRST ===")

    test_bot = bot.SwingBot.__new__(bot.SwingBot)
    test_bot.last_hour_close = {}

    symbol = "ETHUSDT"
    oco_id = 88801
    market_sell_placed = False

    class MockClient:
        filters = {
            "ETHUSDT": {
                "LOT_SIZE": {"minQty": "0.001", "stepSize": "0.001"},
                "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"minNotional": "10.0"},
            }
        }
        def price(self, sym):
            # Market price fell far below stop price of 2900 (e.g. 2800.0)
            return 2800.0

        def account(self):
            return {
                "balances": [
                    {"asset": "USDT", "free": "998.95", "locked": "0.0"},
                    {"asset": "ETH", "free": "0.0", "locked": "0.0"},
                    {"asset": "BNB", "free": "0.1", "locked": "0.0"},
                ]
            }

        def request(self, method, endpoint, params=None, signed=False):
            if endpoint == "/api/v3/account":
                return self.account()
            if endpoint == "/api/v3/myTrades":
                return []
            if endpoint == "/api/v3/orderList":
                return {
                    "orderListId": oco_id,
                    "symbol": symbol,
                    "listStatusType": "ALL_DONE",
                    "listOrderStatus": "ALL_DONE",
                    "orders": [{"orderId": 21}, {"orderId": 22}]
                }
            raise ValueError(f"Unexpected request: {method} {endpoint}")

        def query_order(self, sym, order_id):
            if order_id == 21:
                # Stop loss leg triggered and filled on Binance
                return {
                    "orderId": 21,
                    "symbol": symbol,
                    "orderListId": oco_id,
                    "side": "SELL",
                    "type": "STOP_LOSS_LIMIT",
                    "status": "FILLED",
                    "origQty": "0.01",
                    "executedQty": "0.01",
                    "cummulativeQuoteQty": "28.95"  # Filled at ~2895.0
                }
            elif order_id == 22:
                # Take profit leg expired
                return {
                    "orderId": 22,
                    "symbol": symbol,
                    "orderListId": oco_id,
                    "side": "SELL",
                    "type": "LIMIT_MAKER",
                    "status": "EXPIRED",
                    "origQty": "0.01",
                    "executedQty": "0",
                    "cummulativeQuoteQty": "0"
                }
            raise ValueError(f"Unknown orderId {order_id}")

        def order(self, payload):
            nonlocal market_sell_placed
            market_sell_placed = True
            raise AssertionError(f"Market sell placed when OCO was already ALL_DONE! Payload: {payload}")

    test_bot.client = MockClient()

    # Do NOT mock set_flat_baseline; run unmocked

    # Setup open live position with OCO
    with bot.state.lock:
        bot.state.data["positions"] = {
            symbol: {
                "symbol": symbol,
                "quantity": "0.01",
                "entry_price": 3000.0,
                "stop_price": 2900.0,
                "target_price": 3200.0,
                "entry_order_id": 50,
                "oco_order_list_id": oco_id,
                "oco_covered_quantity": "0.01",
                "unprotected_quantity": "0",
                "opened_at": time.time(),
                "is_paper": False,
            }
        }
        bot.state.data["trades"] = [{
            "symbol": symbol,
            "status": "OPEN",
            "entry": 3000.0,
            "stop": 2900.0,
            "target": 3200.0,
            "qty": "0.01",
            "order_id": 50,
            "oco_id": oco_id,
            "time": time.time(),
        }]
        bot.state.data["manual_pause"] = False
        bot.state.data["recovery_required"] = None
        bot.state.save()

    print("[1] Running reconcile() with market price below stop (2800 < 2900) but OCO status ALL_DONE...")
    test_bot.reconcile()

    assert market_sell_placed is False, "CRITICAL ERROR: Watchdog executed market sell despite OCO being ALL_DONE!"
    print("    VERIFIED: No market sell was placed.")

    with bot.state.lock:
        assert symbol not in bot.state.data["positions"], "Position was not reconciled / removed from state!"
        trade = bot.state.data["trades"][0]
        assert trade["status"] in ("CLOSED", "CLOSED_OCO_STOP"), f"Expected CLOSED_OCO_STOP, got {trade['status']}"
        assert trade["exit"] == 2895.0, f"Expected exit at 2895.0, got {trade['exit']}"
        actual_baseline = bot.state.data.get("last_account_equity_usdt")
        assert actual_baseline == 998.95, f"Expected baseline 998.95 (1000 - 1.05), got {actual_baseline}"
        print(f"    VERIFIED: Position reconciled cleanly via OCO: status={trade['status']}, exit={trade['exit']}, baseline={actual_baseline}")

    print("=== FIX 8 TEST PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()
