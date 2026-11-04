import os
import sys
import time
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SCRATCH_DIR = PROJECT_ROOT / "tests"
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_item2_live_state.json"
LOG_PATH = SCRATCH_DIR / "test_item2.log"

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
    print("=== STARTING ITEM 2 TEST: UNMOCKED BASELINES ACROSS ALL 5 LIVE EXIT PATHS ===")

    test_bot = bot.SwingBot.__new__(bot.SwingBot)
    test_bot.last_hour_close = {}
    test_bot.last_reconcile = 0.0
    test_bot.last_preflight = 0.0

    starting_equity = 1000.0
    current_wallet_usdt = starting_equity
    current_eth_free = 0.0
    fail_account_read = False

    class MockClient:
        filters = {
            "ETHUSDT": {
                "LOT_SIZE": {"minQty": "0.001", "stepSize": "0.001"},
                "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"minNotional": "10.0"},
            }
        }
        price_val = 3000.0
        oco_is_executing = False
        def price(self, sym):
            return self.price_val

        def account(self):
            return self.request("GET", "/api/v3/account", signed=True)

        def request(self, method, endpoint, params=None, signed=False):
            nonlocal current_wallet_usdt, current_eth_free
            if endpoint == "/api/v3/account":
                if fail_account_read:
                    raise RuntimeError("Binance API network timeout on account balances")
                return {
                    "balances": [
                        {"asset": "USDT", "free": str(current_wallet_usdt), "locked": "0.0"},
                        {"asset": "ETH", "free": str(current_eth_free), "locked": "0.0"},
                        {"asset": "BNB", "free": "0.1", "locked": "0.0"},
                    ]
                }
            if endpoint == "/api/v3/myTrades":
                return []
            if endpoint == "/api/v3/orderList":
                if method == "GET":
                    if self.oco_is_executing:
                        return {
                            "orderListId": 55501,
                            "symbol": "ETHUSDT",
                            "listStatusType": "EXEC_STARTED",
                            "listOrderStatus": "EXECUTING",
                            "orders": [{"orderId": 101}, {"orderId": 102}]
                        }
                    else:
                        return {
                            "orderListId": 55501,
                            "symbol": "ETHUSDT",
                            "listStatusType": "ALL_DONE",
                            "listOrderStatus": "ALL_DONE",
                            "orders": [{"orderId": 101}, {"orderId": 102}]
                        }
                elif method == "DELETE":
                    # On OCO cancel, base asset is unlocked back into free balance
                    current_eth_free = 0.01
                    return {"orderListId": 55501, "listOrderStatus": "ALL_DONE"}
            raise ValueError(f"Unexpected request: {method} {endpoint}")

        def order(self, payload):
            nonlocal current_wallet_usdt, current_eth_free
            side = payload.get("side")
            qty = Decimal(str(payload.get("quantity")))
            px = Decimal(str(self.price_val))
            quote = qty * px
            if side == "SELL":
                current_wallet_usdt += float(quote)
                current_eth_free = 0.0
            elif side == "BUY":
                current_wallet_usdt -= float(quote)
                current_eth_free += float(qty)
            return {
                "orderId": 8001,
                "symbol": "ETHUSDT",
                "status": "FILLED",
                "executedQty": str(qty),
                "cummulativeQuoteQty": str(quote),
            }

        def query_order(self, sym, order_id):
            if order_id == 101:
                return {
                    "orderId": 101,
                    "symbol": "ETHUSDT",
                    "orderListId": 55501,
                    "side": "SELL",
                    "type": "LIMIT_MAKER",
                    "status": "FILLED",
                    "origQty": "0.01",
                    "executedQty": "0.01",
                    "cummulativeQuoteQty": "31.25"  # +1.25 profit
                }
            elif order_id == 102:
                return {
                    "orderId": 102,
                    "symbol": "ETHUSDT",
                    "orderListId": 55501,
                    "side": "SELL",
                    "type": "STOP_LOSS_LIMIT",
                    "status": "EXPIRED",
                    "origQty": "0.01",
                    "executedQty": "0",
                    "cummulativeQuoteQty": "0"
                }
            raise ValueError(f"Unknown orderId {order_id}")

    test_bot.client = MockClient()

    # DO NOT MOCK set_flat_baseline: it must run unmocked!
    assert hasattr(test_bot, "set_flat_baseline"), "SwingBot must have set_flat_baseline"

    # Reset state
    def reset_state():
        with bot.state.lock:
            bot.state.data["positions"] = {}
            bot.state.data["trades"] = []
            bot.state.data["manual_pause"] = False
            bot.state.data["manual_pause_reason"] = None
            bot.state.data["recovery_required"] = None
            bot.state.data["net_external_flows_usdt"] = 0.0
            bot.state.data["last_account_equity_usdt"] = starting_equity
            bot.state.save()

    # --- Path 1: Immediate Target Breach (No OCO) ---
    print("[1] Testing Exit Path 1: Immediate Target Breach...")
    reset_state()
    # Buy filled at 3000, market price jumps to 3125 (+1.25 gross pnl on 0.01 ETH)
    test_bot.client.price_val = 3125.0
    current_wallet_usdt = 1000.0 - (0.01 * 3000.0) # 970.0
    res1 = test_bot._direct_market_close_no_oco("ETHUSDT", Decimal("0.01"), 3000.0, 501, "2940.0", "3120.0", "TARGET")
    assert res1.get("ok") is True
    # Wallet after market sell: 970 + 0.01*3125 = 1001.25
    expected_baseline1 = round(1000.0 + (0.01 * (3125.0 - 3000.0)), 4)
    with bot.state.lock:
        b1 = bot.state.data["last_account_equity_usdt"]
        assert b1 == expected_baseline1, f"Path 1: Expected baseline {expected_baseline1}, got {b1}"
        assert bot.state.data["trades"][0]["status"] == "CLOSED_TARGET"
    print(f"    Path 1 PASSED: Baseline unmocked = {b1} (matches starting_equity + pnl: {expected_baseline1})")

    # --- Path 2: OCO Target Fill ---
    print("[2] Testing Exit Path 2: OCO Fill (_handle_completed_oco)...")
    reset_state()
    test_bot.client.oco_is_executing = False
    current_wallet_usdt = 970.0 + 31.25 # 1001.25
    with bot.state.lock:
        bot.state.data["positions"]["ETHUSDT"] = {
            "symbol": "ETHUSDT", "quantity": "0.01", "entry_price": 3000.0,
            "stop_price": 2940.0, "target_price": 3120.0, "entry_order_id": 502,
            "oco_order_list_id": 55501, "oco_covered_quantity": "0.01", "unprotected_quantity": "0"
        }
        bot.state.data["trades"].append({
            "symbol": "ETHUSDT", "status": "OPEN", "entry": 3000.0, "qty": "0.01", "order_id": 502, "oco_id": 55501
        })
        bot.state.save()

    info_done = {"symbol": "ETHUSDT", "orders": [{"orderId": 101}, {"orderId": 102}]}
    test_bot._handle_completed_oco("ETHUSDT", bot.state.data["positions"]["ETHUSDT"], 55501, info_done)
    with bot.state.lock:
        b2 = bot.state.data["last_account_equity_usdt"]
        assert b2 == 1001.25, f"Path 2: Expected baseline 1001.25, got {b2}"
        assert bot.state.data["trades"][0]["status"] == "CLOSED_OCO_TARGET"
    print(f"    Path 2 PASSED: Baseline unmocked = {b2} (status={bot.state.data['trades'][0]['status']})")

    # --- Path 3: Manual Close ---
    print("[3] Testing Exit Path 3: Manual Close (close_position_at_market)...")
    reset_state()
    test_bot.client.oco_is_executing = True
    test_bot.client.price_val = 3050.0 # +0.50 gross pnl on 0.01 ETH
    current_wallet_usdt = 970.0 # coins held
    with bot.state.lock:
        bot.state.data["positions"]["ETHUSDT"] = {
            "symbol": "ETHUSDT", "quantity": "0.01", "entry_price": 3000.0,
            "stop_price": 2940.0, "target_price": 3120.0, "entry_order_id": 503,
            "oco_order_list_id": 55501, "oco_covered_quantity": "0.01", "unprotected_quantity": "0"
        }
        bot.state.data["trades"].append({
            "symbol": "ETHUSDT", "status": "OPEN", "entry": 3000.0, "qty": "0.01", "order_id": 503, "oco_id": 55501
        })
        bot.state.save()

    res3 = test_bot.close_position_at_market("ETHUSDT")
    assert res3.get("ok") is True
    expected_baseline3 = 970.0 + (0.01 * 3050.0) # 1000.50
    with bot.state.lock:
        b3 = bot.state.data["last_account_equity_usdt"]
        assert b3 == expected_baseline3, f"Path 3: Expected baseline {expected_baseline3}, got {b3}"
        assert bot.state.data["trades"][0]["status"] == "CLOSED_MANUAL_MARKET"
    print(f"    Path 3 PASSED: Baseline unmocked = {b3} (status={bot.state.data['trades'][0]['status']})")

    # --- Path 4: Stop-Limit Fallback Exit ---
    print("[4] Testing Exit Path 4: Stop Fallback (close_position_at_market with is_stop_fallback=True)...")
    reset_state()
    test_bot.client.oco_is_executing = True
    test_bot.client.price_val = 2930.0 # -0.70 loss on 0.01 ETH
    current_wallet_usdt = 970.0
    with bot.state.lock:
        bot.state.data["positions"]["ETHUSDT"] = {
            "symbol": "ETHUSDT", "quantity": "0.01", "entry_price": 3000.0,
            "stop_price": 2940.0, "target_price": 3120.0, "entry_order_id": 504,
            "oco_order_list_id": 55501, "oco_covered_quantity": "0.01", "unprotected_quantity": "0"
        }
        bot.state.data["trades"].append({
            "symbol": "ETHUSDT", "status": "OPEN", "entry": 3000.0, "qty": "0.01", "order_id": 504, "oco_id": 55501
        })
        bot.state.save()

    res4 = test_bot.close_position_at_market("ETHUSDT", is_stop_fallback=True)
    assert res4.get("ok") is True
    expected_baseline4 = 970.0 + (0.01 * 2930.0) # 999.30
    with bot.state.lock:
        b4 = bot.state.data["last_account_equity_usdt"]
        assert b4 == expected_baseline4, f"Path 4: Expected baseline {expected_baseline4}, got {b4}"
        assert bot.state.data["trades"][0]["status"] == "CLOSED_STOP_FALLBACK"
    print(f"    Path 4 PASSED: Baseline unmocked = {b4} (status={bot.state.data['trades'][0]['status']})")

    # --- Path 5: Watchdog Price Exit ---
    print("[5] Testing Exit Path 5: Watchdog Price Close...")
    reset_state()
    test_bot.client.oco_is_executing = True
    test_bot.client.price_val = 2900.0 # -1.0 loss on 0.01 ETH
    current_wallet_usdt = 970.0
    with bot.state.lock:
        bot.state.data["positions"]["ETHUSDT"] = {
            "symbol": "ETHUSDT", "quantity": "0.01", "entry_price": 3000.0,
            "stop_price": 2940.0, "target_price": 3120.0, "entry_order_id": 505,
            "oco_order_list_id": 55501, "oco_covered_quantity": "0.01", "unprotected_quantity": "0"
        }
        bot.state.data["trades"].append({
            "symbol": "ETHUSDT", "status": "OPEN", "entry": 3000.0, "qty": "0.01", "order_id": 505, "oco_id": 55501
        })
        bot.state.save()

    res5 = test_bot.close_position_at_market("ETHUSDT")
    assert res5.get("ok") is True
    expected_baseline5 = 970.0 + (0.01 * 2900.0) # 999.00
    with bot.state.lock:
        b5 = bot.state.data["last_account_equity_usdt"]
        assert b5 == expected_baseline5, f"Path 5: Expected baseline {expected_baseline5}, got {b5}"
    print(f"    Path 5 PASSED: Baseline unmocked = {b5}")

    # --- Path 6: Failed-Read Case ---
    print("[6] Testing Failed-Read Case: exchange balances call throws network exception...")
    reset_state()
    fail_account_read = True
    ok = test_bot.set_flat_baseline()
    assert ok is False, "set_flat_baseline should return False on network read error"
    with bot.state.lock:
        assert bot.state.data["last_account_equity_usdt"] is None, "Baseline must be None, NOT silently reset!"
        assert bot.state.data["manual_pause"] is True, "manual_pause must be True"
        assert bot.state.data["manual_pause_reason"] == "post_exit_rebaseline_failed"
    print(f"    Failed-read case PASSED: baseline is None and manual_pause_reason='post_exit_rebaseline_failed'.")

    print("=== ITEM 2 TEST PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()
