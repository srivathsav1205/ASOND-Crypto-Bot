import os
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

SCRATCH_DIR = Path("tests")
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_fix9_live_state.json"
LOG_PATH = SCRATCH_DIR / "test_fix9.log"

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
    print("=== STARTING FIX 9 TEST: TRIGGERED UNFILLED STOP-LIMIT FALLBACK EXIT (5S CADENCE) ===")

    test_bot = bot.SwingBot.__new__(bot.SwingBot)
    test_bot.last_hour_close = {}
    test_bot.last_reconcile = 0.0
    test_bot.last_preflight = 0.0

    symbol = "ETHUSDT"
    oco_id = 77701
    market_sell_placed = False

    class MockClient:
        filters = {
            "ETHUSDT": {
                "LOT_SIZE": {"minQty": "0.001", "stepSize": "0.001"},
                "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"minNotional": "10.0"},
            }
        }
        wallet_usdt = 970.0
        wallet_eth = "0.0"

        def account(self):
            return {
                "balances": [
                    {"asset": "USDT", "free": str(self.wallet_usdt), "locked": "0.0"},
                    {"asset": "ETH", "free": str(self.wallet_eth), "locked": "0.0"},
                    {"asset": "BNB", "free": "0.1", "locked": "0.0"},
                ]
            }

        def price(self, sym):
            return 2930.0

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
                        "orders": [{"orderId": 31}, {"orderId": 32}]
                    }
                elif method == "DELETE":
                    # On OCO cancel, coins unlock into free wallet balance
                    self.wallet_eth = "0.01"
                    return {"orderListId": oco_id, "listOrderStatus": "ALL_DONE"}
            raise ValueError(f"Unexpected request: {method} {endpoint}")

        def query_order(self, sym, order_id):
            if order_id == 31:
                return {
                    "orderId": 31,
                    "symbol": symbol,
                    "orderListId": oco_id,
                    "side": "SELL",
                    "type": "STOP_LOSS_LIMIT",
                    "status": "NEW",
                    "origQty": "0.01",
                    "executedQty": "0",
                    "cummulativeQuoteQty": "0"
                }
            elif order_id == 32:
                return {
                    "orderId": 32,
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
            self.wallet_eth = "0.0"
            self.wallet_usdt += float(payload["quantity"]) * 2930.0
            return {
                "orderId": 6001,
                "symbol": symbol,
                "status": "FILLED",
                "executedQty": payload["quantity"],
                "cummulativeQuoteQty": str(float(payload["quantity"]) * 2930.0)
            }

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
                "entry_order_id": 40,
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
            "order_id": 40,
            "oco_id": oco_id,
            "time": time.time(),
        }]
        bot.state.data["manual_pause"] = False
        bot.state.data["recovery_required"] = None
        bot.state.save()

    # Pass 1: t = 0 (stop triggered) -> start timer
    print("[1] Pass 1 (t=0s): Reconciling immediately after stop trigger...")
    test_bot.reconcile()
    assert market_sell_placed is False, "Market sell should NOT trigger at t=0s"
    with bot.state.lock:
        triggered_at = bot.state.data["positions"][symbol].get("stop_triggered_at")
        assert triggered_at is not None
    print(f"    VERIFIED: Timer started at {triggered_at}")

    # Pass 2: t = 10s (reconcile pass at 10s) -> still waiting
    print("[2] Pass 2 (t=10s): Reconcile pass within 30s window...")
    with bot.state.lock:
        bot.state.data["positions"][symbol]["stop_triggered_at"] = time.time() - 10.0
        bot.state.save()
    test_bot.reconcile()
    assert market_sell_placed is False, "Market sell should NOT trigger at t=10s"
    print("    VERIFIED: Position kept open waiting for fill at t=10s.")

    # Pass 3: t = 20s (reconcile pass at 20s) -> still waiting
    print("[3] Pass 3 (t=20s): Reconcile pass within 30s window...")
    with bot.state.lock:
        bot.state.data["positions"][symbol]["stop_triggered_at"] = time.time() - 20.0
        bot.state.save()
    test_bot.reconcile()
    assert market_sell_placed is False, "Market sell should NOT trigger at t=20s"
    print("    VERIFIED: Position kept open waiting for fill at t=20s.")

    # Pass 4: t = 35s (reconcile pass at 35s >= 30s) -> triggers fallback market exit
    print("[4] Pass 4 (t=35s): Reconcile pass at >= 30s timeout...")
    with bot.state.lock:
        bot.state.data["positions"][symbol]["stop_triggered_at"] = time.time() - 35.0
        bot.state.save()
    test_bot.reconcile()
    assert market_sell_placed is True, "Fallback market sell MUST trigger after >= 30s timeout!"
    print("    VERIFIED: Fallback market sell triggered cleanly on fast cadence.")

    with bot.state.lock:
        assert symbol not in bot.state.data["positions"]
        trade = bot.state.data["trades"][0]
        assert trade["status"] == "CLOSED_STOP_FALLBACK", f"Expected CLOSED_STOP_FALLBACK, got {trade['status']}"
        assert trade["exit"] == 2930.0
        actual_baseline = bot.state.data.get("last_account_equity_usdt")
        assert actual_baseline == 999.30, f"Expected baseline 999.30, got {actual_baseline}"
    print(f"    VERIFIED: Position closed at exit={trade['exit']}, status={trade['status']}, baseline={actual_baseline}")

    print("=== FIX 9 TEST PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()
