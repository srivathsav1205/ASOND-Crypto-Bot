import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SCRATCH_DIR = PROJECT_ROOT / "tests"
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_time_stop_live_state.json"
LOG_PATH = SCRATCH_DIR / "test_time_stop.log"

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
    print("=== STARTING LIVE TIME STOP TEST (enforce_time_stop) ===")
    bot.MAX_HOLD_HOURS = 24.0

    test_bot = bot.SwingBot.__new__(bot.SwingBot)
    test_bot.last_hour_close = {}
    test_bot.last_reconcile = 0.0

    symbol = "ETHUSDT"
    oco_id = 44401
    market_sell_called = False

    class MockClient:
        filters = {
            "ETHUSDT": {
                "LOT_SIZE": {"minQty": "0.001", "stepSize": "0.001"},
                "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"minNotional": "10.0"},
            }
        }
        def price(self, sym):
            return 3020.0

        def account(self):
            return {
                "balances": [
                    {"asset": "USDT", "free": "1000.0", "locked": "0.0"},
                    {"asset": "ETH", "free": "0.01", "locked": "0.0"},
                    {"asset": "BNB", "free": "0.1", "locked": "0.0"},
                ]
            }

        def request(self, method, endpoint, params=None, signed=False):
            if endpoint == "/api/v3/account":
                return self.account()
            if endpoint == "/api/v3/myTrades":
                return []
            if endpoint == "/api/v3/orderList":
                if method == "GET":
                    return {
                        "orderListId": oco_id,
                        "symbol": symbol,
                        "listStatusType": "EXEC_STARTED",
                        "listOrderStatus": "EXECUTING",
                        "orders": [{"orderId": 401}, {"orderId": 402}]
                    }
                elif method == "DELETE":
                    return {"orderListId": oco_id, "listOrderStatus": "ALL_DONE"}
            raise ValueError(f"Unexpected request: {method} {endpoint}")

        def order(self, payload):
            nonlocal market_sell_called
            market_sell_called = True
            return {
                "orderId": 7001,
                "symbol": symbol,
                "status": "FILLED",
                "executedQty": payload["quantity"],
                "cummulativeQuoteQty": str(float(payload["quantity"]) * 3020.0)
            }

    test_bot.client = MockClient()

    # Pass 1: Position opened 10 hours ago (< 24h) -> should NOT trigger time stop
    print("[1] Position opened 10h ago (< 24h MAX_HOLD_HOURS): running enforce_time_stop...")
    now = time.time()
    with bot.state.lock:
        bot.state.data["positions"] = {
            symbol: {
                "symbol": symbol,
                "quantity": "0.01",
                "entry_price": 3000.0,
                "stop_price": 2900.0,
                "target_price": 3200.0,
                "entry_order_id": 400,
                "oco_order_list_id": oco_id,
                "oco_covered_quantity": "0.01",
                "unprotected_quantity": "0",
                "opened_at": now - (10 * 3600),  # 10h ago
                "is_paper": False,
            }
        }
        bot.state.data["trades"] = [{
            "symbol": symbol, "status": "OPEN", "entry": 3000.0, "qty": "0.01", "order_id": 400, "oco_id": oco_id
        }]
        bot.state.data["manual_pause"] = False
        bot.state.data["recovery_required"] = None
        bot.state.data["last_account_equity_usdt"] = 1000.0
        bot.state.save()

    test_bot.enforce_time_stop()
    assert market_sell_called is False, "Time stop should NOT have triggered at 10 hours!"
    assert symbol in bot.state.data["positions"], "Position should remain open"
    print("    PASSED: Position retained at 10h held.")

    # Pass 2: Position opened 24.5 hours ago (>= 24h) -> MUST trigger time stop!
    print("[2] Position opened 24.5h ago (>= 24h MAX_HOLD_HOURS): running enforce_time_stop...")
    with bot.state.lock:
        bot.state.data["positions"][symbol]["opened_at"] = now - (24.5 * 3600)
        bot.state.save()

    test_bot.enforce_time_stop()
    assert market_sell_called is True, "Time stop MUST have triggered market sell at >= 24h!"
    with bot.state.lock:
        assert symbol not in bot.state.data["positions"], "Position must be removed after time stop exit!"
        trade = bot.state.data["trades"][0]
        assert trade["status"] == "CLOSED_TIME_STOP", f"Expected CLOSED_TIME_STOP, got {trade['status']}"
        assert trade["exit"] == 3020.0
    print(f"    PASSED: Position closed at market via time stop with status={trade['status']}.")

    print("=== LIVE TIME STOP TEST PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()