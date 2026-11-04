import os
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRATCH_DIR = ROOT / "tests"
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_unprotected_live_state.json"
LOG_PATH = SCRATCH_DIR / "test_unprotected.log"

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
    print("=== STARTING UNPROTECTED WINDOW TEST (CANCEL SUCCESS + SELL FAILURE) ===")

    test_bot = bot.SwingBot.__new__(bot.SwingBot)
    test_bot.last_hour_close = {}

    symbol = "ETHUSDT"
    oco_id = 99955

    class MockClient:
        filters = {
            "ETHUSDT": {
                "LOT_SIZE": {"minQty": "0.001", "stepSize": "0.001"},
                "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"minNotional": "10.0"},
            }
        }

        def __init__(self):
            self.wallet_usdt = 970.0
            self.wallet_eth_free = "0.01"
            self.fail_market_sell = True
            self.allow_emergency_stop = False
            self.orders_placed = []

        def account(self):
            return {
                "balances": [
                    {"asset": "USDT", "free": str(self.wallet_usdt), "locked": "0.0"},
                    {"asset": "ETH", "free": str(self.wallet_eth_free), "locked": "0.0"},
                    {"asset": "BNB", "free": "0.1", "locked": "0.0"},
                ]
            }

        def price(self, sym):
            return 3000.0

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
                        "orders": [{"orderId": 11}, {"orderId": 12}]
                    }
                elif method == "DELETE":
                    return {
                        "orderListId": oco_id,
                        "symbol": symbol,
                        "listOrderStatus": "ALL_DONE",
                        "orders": [{"orderId": 11}, {"orderId": 12}],
                        "orderReports": [
                            {"orderId": 11, "symbol": symbol, "status": "CANCELED", "executedQty": "0.00"},
                            {"orderId": 12, "symbol": symbol, "status": "CANCELED", "executedQty": "0.00"},
                        ]
                    }
            raise ValueError(f"Unexpected request: {method} {endpoint}")

        def order(self, payload):
            self.orders_placed.append(payload)
            if payload.get("type") == "MARKET" and self.fail_market_sell:
                raise RuntimeError("Binance API error: simulated network failure on market sell")
            if payload.get("type") == "STOP_LOSS_LIMIT":
                if not self.allow_emergency_stop:
                    raise RuntimeError("Binance API error: simulated emergency stop placement failure")
                return {"orderId": 777123, "symbol": symbol, "status": "NEW"}
            raise ValueError(f"Unexpected order payload: {payload}")

    def reset_state():
        with bot.state.lock:
            bot.state.data["positions"] = {
                symbol: {
                    "symbol": symbol,
                    "quantity": "0.01",
                    "entry_price": 3000.0,
                    "stop_price": 2900.0,
                    "target_price": 3200.0,
                    "entry_order_id": 100,
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
                "order_id": 100,
                "oco_id": oco_id,
                "time": time.time(),
            }]
            bot.state.data["manual_pause"] = False
            bot.state.data["recovery_required"] = None
            bot.state.save()

    # Case A: Market sell fails, emergency stop ALSO fails -> UNPROTECTED WINDOW active!
    print("[1] Testing Unprotected Window: Market sell fails and emergency stop fails...")
    reset_state()
    test_bot.client = MockClient()
    test_bot.client.allow_emergency_stop = False

    failed_a = False
    try:
        test_bot.close_position_at_market(symbol)
    except RuntimeError as e:
        failed_a = True
        print(f"    Caught expected runtime error: {e}")

    assert failed_a is True, "Must raise RuntimeError when close fails"
    with bot.state.lock:
        rec = bot.state.data.get("recovery_required")
        assert rec is not None, "recovery_required must be set"
        assert rec.get("unprotected_window") is True, f"Expected unprotected_window=True, got {rec}"
        assert rec.get("emergency_protection_placed") is False
        assert "UNPROTECTED in spot wallet" in rec.get("reason")
        print(f"    PASSED: Flagged unprotected window in recovery lock: {rec['reason']}")

    # Case B: Market sell fails, but emergency stop SUCCEEDS -> position is protected
    print("\n[2] Testing Emergency Stop Protection: Market sell fails, emergency stop succeeds...")
    reset_state()
    test_bot.client = MockClient()
    test_bot.client.allow_emergency_stop = True

    failed_b = False
    try:
        test_bot.close_position_at_market(symbol)
    except RuntimeError as e:
        failed_b = True
        print(f"    Caught expected runtime error: {e}")

    assert failed_b is True
    with bot.state.lock:
        rec = bot.state.data.get("recovery_required")
        assert rec is not None
        assert rec.get("unprotected_window") is False
        assert rec.get("emergency_protection_placed") is True
        assert rec.get("emergency_order_id") == 777123
        print(f"    PASSED: Emergency stop placed on exchange: {rec['reason']}")

    print("\n=== UNPROTECTED WINDOW TEST PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()
