import os
import sys
import time
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SCRATCH_DIR = PROJECT_ROOT / "tests"
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_item3_live_state.json"
LOG_PATH = SCRATCH_DIR / "test_item3.log"

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
    print("=== STARTING ITEM 3 TEST: SUCCESSFUL CLOSE CLEARS ONLY ITS OWN LOCK ===")

    test_bot = bot.SwingBot.__new__(bot.SwingBot)
    test_bot.last_hour_close = {}
    test_bot.last_reconcile = 0.0

    class MockClient:
        filters = {
            "ETHUSDT": {
                "LOT_SIZE": {"minQty": "0.001", "stepSize": "0.001"},
                "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"minNotional": "10.0"},
            },
            "SOLUSDT": {
                "LOT_SIZE": {"minQty": "0.01", "stepSize": "0.01"},
                "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"minNotional": "10.0"},
            }
        }
        prices = {"ETHUSDT": 3000.0, "SOLUSDT": 150.0}

        def price(self, sym):
            return self.prices.get(sym, 100.0)

        def account(self):
            return {
                "balances": [
                    {"asset": "USDT", "free": "1000.0", "locked": "0.0"},
                    {"asset": "ETH", "free": "0.01", "locked": "0.0"},
                    {"asset": "SOL", "free": "0.2", "locked": "0.0"},
                    {"asset": "BNB", "free": "0.1", "locked": "0.0"},
                ]
            }

        def request(self, method, endpoint, params=None, signed=False):
            if endpoint == "/api/v3/account":
                return self.account()
            if endpoint == "/api/v3/myTrades":
                return []
            if endpoint == "/api/v3/orderList":
                olid = (params or {}).get("orderListId", 88801)
                sym = "ETHUSDT" if olid == 77701 else "SOLUSDT"
                if method == "GET":
                    return {
                        "orderListId": olid,
                        "symbol": sym,
                        "listStatusType": "EXEC_STARTED",
                        "listOrderStatus": "EXECUTING",
                        "orders": [{"orderId": 801}, {"orderId": 802}]
                    }
                elif method == "DELETE":
                    return {"orderListId": olid, "listOrderStatus": "ALL_DONE"}
            raise ValueError(f"Unexpected request: {method} {endpoint}")

        def order(self, payload):
            sym = payload.get("symbol")
            qty = payload.get("quantity")
            px = self.prices[sym]
            return {
                "orderId": 9001,
                "symbol": sym,
                "status": "FILLED",
                "executedQty": qty,
                "cummulativeQuoteQty": str(float(qty) * px),
            }

    test_bot.client = MockClient()

    # Step 1: Set up open positions for Symbol A (ETHUSDT) and Symbol B (SOLUSDT)
    # Failed sell on ETHUSDT sets recovery_required for ETHUSDT
    print("[1] Setting recovery_required lock on Symbol A (ETHUSDT)...")
    with bot.state.lock:
        bot.state.data["positions"] = {
            "ETHUSDT": {
                "symbol": "ETHUSDT", "quantity": "0.01", "entry_price": 3000.0,
                "stop_price": 2900.0, "target_price": 3200.0, "entry_order_id": 101,
                "oco_order_list_id": 77701, "oco_covered_quantity": "0.01", "unprotected_quantity": "0"
            },
            "SOLUSDT": {
                "symbol": "SOLUSDT", "quantity": "0.2", "entry_price": 140.0,
                "stop_price": 135.0, "target_price": 155.0, "entry_order_id": 102,
                "oco_order_list_id": 88801, "oco_covered_quantity": "0.2", "unprotected_quantity": "0"
            }
        }
        bot.state.data["trades"] = [
            {"symbol": "ETHUSDT", "status": "OPEN", "entry": 3000.0, "qty": "0.01", "order_id": 101, "oco_id": 77701},
            {"symbol": "SOLUSDT", "status": "OPEN", "entry": 140.0, "qty": "0.2", "order_id": 102, "oco_id": 88801},
        ]
        bot.state.data["manual_pause"] = True
        bot.state.data["manual_pause_reason"] = "market_sell_failed"
        bot.state.data["recovery_required"] = {
            "symbol": "ETHUSDT",
            "quantity": "0.01",
            "oco_order_list_id": 77701,
            "reason": "Manual market sell failed; OCO left active and intact. Reconcile exchange before resuming.",
            "time": time.time()
        }
        bot.state.data["last_account_equity_usdt"] = 1000.0
        bot.state.save()

    # Step 2: Now Symbol B (SOLUSDT) performs a successful close_position_at_market
    print("[2] Executing successful close_position_at_market for Symbol B (SOLUSDT)...")
    res_b = test_bot.close_position_at_market("SOLUSDT")
    assert res_b.get("ok") is True, f"SOLUSDT close failed: {res_b}"

    # Assert: Symbol B position is removed, but Symbol A's recovery lock is STILL ACTIVE!
    with bot.state.lock:
        assert "SOLUSDT" not in bot.state.data["positions"], "SOLUSDT should be closed"
        assert "ETHUSDT" in bot.state.data["positions"], "ETHUSDT position must remain"
        rec_lock = bot.state.data.get("recovery_required")
        assert rec_lock is not None, "CRITICAL ERROR: Symbol B close cleared recovery_required lock!"
        assert isinstance(rec_lock, dict) and rec_lock.get("symbol") == "ETHUSDT", \
            f"recovery_required should still belong to ETHUSDT, but got {rec_lock}"
        assert bot.state.data.get("manual_pause") is True
    print("    VERIFIED: Symbol B (SOLUSDT) closed successfully and Symbol A (ETHUSDT) recovery lock remains active!")

    # Step 3: Now test _handle_completed_oco for another symbol C while ETHUSDT lock is active
    print("[3] Testing _handle_completed_oco for Symbol C while ETHUSDT lock is active...")
    with bot.state.lock:
        bot.state.data["positions"]["BTCUSDT"] = {
            "symbol": "BTCUSDT", "quantity": "0.001", "entry_price": 60000.0,
            "stop_price": 58000.0, "target_price": 64000.0, "entry_order_id": 103,
            "oco_order_list_id": 99901, "oco_covered_quantity": "0.001", "unprotected_quantity": "0"
        }
        bot.state.data["trades"].append({
            "symbol": "BTCUSDT", "status": "OPEN", "entry": 60000.0, "qty": "0.001", "order_id": 103, "oco_id": 99901
        })
        bot.state.save()

    completed_oco_info = {
        "symbol": "BTCUSDT",
        "orders": [
            {
                "orderId": 701, "symbol": "BTCUSDT", "orderListId": 99901, "side": "SELL", "type": "LIMIT_MAKER",
                "status": "FILLED", "origQty": "0.001", "executedQty": "0.001", "cummulativeQuoteQty": "64.0"
            },
            {
                "orderId": 702, "symbol": "BTCUSDT", "orderListId": 99901, "side": "SELL", "type": "STOP_LOSS_LIMIT",
                "status": "EXPIRED", "origQty": "0.001", "executedQty": "0", "cummulativeQuoteQty": "0"
            }
        ]
    }
    # Mock query_order for BTC child orders
    test_bot.client.query_order = lambda sym, oid: next(o for o in completed_oco_info["orders"] if o["orderId"] == oid)
    test_bot._handle_completed_oco("BTCUSDT", bot.state.data["positions"]["BTCUSDT"], 99901, completed_oco_info)

    with bot.state.lock:
        assert "BTCUSDT" not in bot.state.data["positions"]
        rec_lock = bot.state.data.get("recovery_required")
        assert rec_lock is not None, "CRITICAL ERROR: _handle_completed_oco for BTCUSDT cleared ETHUSDT recovery lock!"
        assert rec_lock.get("symbol") == "ETHUSDT"
    print("    VERIFIED: _handle_completed_oco for BTCUSDT did NOT clear Symbol A (ETHUSDT) recovery lock!")

    # Step 4: Now execute successful close for Symbol A (ETHUSDT)
    print("[4] Executing successful close for Symbol A (ETHUSDT)...")
    res_a = test_bot.close_position_at_market("ETHUSDT")
    assert res_a.get("ok") is True, f"ETHUSDT close failed: {res_a}"

    with bot.state.lock:
        assert "ETHUSDT" not in bot.state.data["positions"]
        rec_lock = bot.state.data.get("recovery_required")
        assert rec_lock is None, f"ETHUSDT's own successful close should clear its recovery lock, got {rec_lock}"
    print("    VERIFIED: Symbol A's own close cleanly cleared its own recovery lock!")

    print("=== ITEM 3 TEST PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()
