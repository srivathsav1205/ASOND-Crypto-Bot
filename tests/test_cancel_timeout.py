import os
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRATCH_DIR = ROOT / "tests"
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_cancel_timeout_live_state.json"
LOG_PATH = SCRATCH_DIR / "test_cancel_timeout.log"

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
    print("=== STARTING CANCEL TIMEOUT RESOLUTION TEST ===")

    test_bot = bot.SwingBot.__new__(bot.SwingBot)
    test_bot.last_hour_close = {}

    symbol = "ETHUSDT"

    # =========================================================================
    # Part 1: DELETE times out; GET /orderList returns only orderId & symbol;
    #         GET /order shows child leg FILLED -> resolves as OCO fill
    # =========================================================================
    print("[1] Executing close_position_at_market where DELETE times out, and GET /order confirms child leg filled...")
    oco_id_1 = 88877

    class MockClientPart1:
        filters = {
            "ETHUSDT": {
                "LOT_SIZE": {"minQty": "0.001", "stepSize": "0.001"},
                "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"minNotional": "10.0"},
            }
        }

        def __init__(self):
            self.wallet_usdt = 970.0
            self.get_orderlist_calls = 0
            self.get_order_calls = []

        def account(self):
            return {
                "balances": [
                    {"asset": "USDT", "free": str(self.wallet_usdt), "locked": "0.0"},
                    {"asset": "BNB", "free": "0.1", "locked": "0.0"},
                ]
            }

        def price(self, sym):
            return 3125.0

        def query_order(self, sym, order_id):
            return self.request("GET", "/api/v3/order", {"symbol": sym, "orderId": order_id})

        def request(self, method, endpoint, params=None, signed=False):
            if endpoint == "/api/v3/account":
                return self.account()
            if endpoint == "/api/v3/myTrades":
                return []
            if endpoint == "/api/v3/order":
                if method == "GET":
                    oid = (params or {}).get("orderId")
                    self.get_order_calls.append(oid)
                    if oid == 21:
                        # Target leg filled
                        return {
                            "orderId": 21,
                            "symbol": symbol,
                            "orderListId": oco_id_1,
                            "side": "SELL",
                            "type": "LIMIT_MAKER",
                            "status": "FILLED",
                            "origQty": "0.01",
                            "executedQty": "0.01",
                            "cummulativeQuoteQty": "31.25"
                        }
                    elif oid == 22:
                        return {
                            "orderId": 22,
                            "symbol": symbol,
                            "orderListId": oco_id_1,
                            "side": "SELL",
                            "type": "STOP_LOSS_LIMIT",
                            "status": "CANCELED",
                            "origQty": "0.01",
                            "executedQty": "0.00",
                            "cummulativeQuoteQty": "0.00"
                        }
                    raise ValueError(f"Unknown orderId {oid}")
            if endpoint == "/api/v3/orderList":
                if method == "GET":
                    self.get_orderlist_calls += 1
                    if self.get_orderlist_calls == 1:
                        # Initial pre-check: OCO is EXECUTING
                        return {
                            "orderListId": oco_id_1,
                            "symbol": symbol,
                            "listStatusType": "EXEC_STARTED",
                            "listOrderStatus": "EXECUTING",
                            "orders": [
                                {"symbol": symbol, "orderId": 21},
                                {"symbol": symbol, "orderId": 22}
                            ]
                        }
                    else:
                        # Re-query after DELETE timeout: OCO reached ALL_DONE on exchange
                        # Real Binance orderList schema: NO status, NO executedQty in orders
                        return {
                            "orderListId": oco_id_1,
                            "symbol": symbol,
                            "listStatusType": "ALL_DONE",
                            "listOrderStatus": "ALL_DONE",
                            "orders": [
                                {"symbol": symbol, "orderId": 21},
                                {"symbol": symbol, "orderId": 22}
                            ]
                        }
                elif method == "DELETE":
                    raise RuntimeError("Binance API error: SocketTimeout (read timed out after 10s)")
            raise ValueError(f"Unexpected request: {method} {endpoint}")

    test_bot.client = MockClientPart1()

    with bot.state.lock:
        bot.state.data["positions"] = {
            symbol: {
                "symbol": symbol,
                "quantity": "0.01",
                "entry_price": 3000.0,
                "stop_price": 2900.0,
                "target_price": 3125.0,
                "entry_order_id": 100,
                "oco_order_list_id": oco_id_1,
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
            "target": 3125.0,
            "qty": "0.01",
            "order_id": 100,
            "oco_id": oco_id_1,
            "time": time.time(),
        }]
        bot.state.data["manual_pause"] = False
        bot.state.data["recovery_required"] = None
        bot.state.save()

    res = test_bot.close_position_at_market(symbol)
    assert res.get("ok") is True, f"Expected successful resolution, got: {res}"
    assert len(test_bot.client.get_order_calls) > 0, "GET /api/v3/order was not called to check leg status!"
    print(f"    PASSED: Verified leg status was fetched via GET /api/v3/order: calls={test_bot.client.get_order_calls}")

    with bot.state.lock:
        assert bot.state.data.get("recovery_required") is None, "False recovery_required lock was placed!"
        assert bot.state.data.get("manual_pause") is False, "manual_pause should remain False"
        assert symbol not in bot.state.data["positions"], "Position should be popped from positions"
        assert bot.state.data["trades"][0]["status"] == "CLOSED_OCO_TARGET"
        assert bot.state.data["trades"][0]["exit"] == 3125.0
        print(f"    PASSED: Trade settled via OCO fill logic: status={bot.state.data['trades'][0]['status']}, exit={bot.state.data['trades'][0]['exit']}")

    # =========================================================================
    # Part 2: DELETE times out; GET /orderList returns only orderId & symbol;
    #         GET /order shows BOTH legs CANCELED (no fill) -> proceeds to market sell
    # =========================================================================
    print("\n[2] Executing close_position_at_market where DELETE times out, and GET /order confirms BOTH legs CANCELED...")
    oco_id_2 = 99922

    class MockClientPart2:
        filters = {
            "ETHUSDT": {
                "LOT_SIZE": {"minQty": "0.001", "stepSize": "0.001"},
                "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"minNotional": "10.0"},
            }
        }

        def __init__(self):
            self.wallet_usdt = 970.0
            self.wallet_eth = "0.01"
            self.market_sell_called = False
            self.get_orderlist_calls = 0
            self.get_order_calls = []

        def account(self):
            return {
                "balances": [
                    {"asset": "USDT", "free": str(self.wallet_usdt), "locked": "0.0"},
                    {"asset": "ETH", "free": str(self.wallet_eth), "locked": "0.0"},
                    {"asset": "BNB", "free": "0.1", "locked": "0.0"},
                ]
            }

        def price(self, sym):
            return 3000.0

        def query_order(self, sym, order_id):
            return self.request("GET", "/api/v3/order", {"symbol": sym, "orderId": order_id})

        def request(self, method, endpoint, params=None, signed=False):
            if endpoint == "/api/v3/account":
                return self.account()
            if endpoint == "/api/v3/myTrades":
                return []
            if endpoint == "/api/v3/order":
                if method == "GET":
                    oid = (params or {}).get("orderId")
                    self.get_order_calls.append(oid)
                    # Both legs canceled with 0 fill
                    return {
                        "symbol": symbol,
                        "orderId": oid,
                        "status": "CANCELED",
                        "executedQty": "0.00",
                        "cummulativeQuoteQty": "0.00"
                    }
            if endpoint == "/api/v3/orderList":
                if method == "GET":
                    self.get_orderlist_calls += 1
                    if self.get_orderlist_calls == 1:
                        # Initial pre-check: OCO is EXECUTING
                        return {
                            "orderListId": oco_id_2,
                            "symbol": symbol,
                            "listStatusType": "EXEC_STARTED",
                            "listOrderStatus": "EXECUTING",
                            "orders": [
                                {"symbol": symbol, "orderId": 31},
                                {"symbol": symbol, "orderId": 32}
                            ]
                        }
                    else:
                        # Re-query after timeout: OCO reached ALL_DONE on exchange
                        # Real Binance orderList schema: NO status, NO executedQty
                        return {
                            "orderListId": oco_id_2,
                            "symbol": symbol,
                            "listStatusType": "ALL_DONE",
                            "listOrderStatus": "ALL_DONE",
                            "orders": [
                                {"symbol": symbol, "orderId": 31},
                                {"symbol": symbol, "orderId": 32}
                            ]
                        }
                elif method == "DELETE":
                    raise RuntimeError("Binance API error: SocketTimeout (read timed out after 10s)")
            raise ValueError(f"Unexpected request: {method} {endpoint}")

        def order(self, params):
            self.market_sell_called = True
            self.wallet_eth = "0.0"
            self.wallet_usdt += 30.0
            return {
                "symbol": symbol,
                "orderId": 7001,
                "status": "FILLED",
                "executedQty": "0.01",
                "cummulativeQuoteQty": "30.00",
                "fills": [{"price": "3000.00", "qty": "0.01"}]
            }

    test_bot.client = MockClientPart2()

    with bot.state.lock:
        bot.state.data["positions"] = {
            symbol: {
                "symbol": symbol,
                "quantity": "0.01",
                "entry_price": 3000.0,
                "stop_price": 2900.0,
                "target_price": 3125.0,
                "entry_order_id": 200,
                "oco_order_list_id": oco_id_2,
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
            "target": 3125.0,
            "qty": "0.01",
            "order_id": 200,
            "oco_id": oco_id_2,
            "time": time.time(),
        }]
        bot.state.data["last_account_equity_usdt"] = 1000.0
        bot.state.data["manual_pause"] = False
        bot.state.data["recovery_required"] = None
        bot.state.save()

    res2 = test_bot.close_position_at_market(symbol)
    assert res2.get("ok") is True, f"Expected successful resolution, got: {res2}"
    assert len(test_bot.client.get_order_calls) == 2, "Both legs were not queried via GET /api/v3/order!"
    assert test_bot.client.market_sell_called is True, "Market sell was NOT called when OCO legs were canceled!"
    print(f"    PASSED: Verified both legs checked via GET /api/v3/order (calls={test_bot.client.get_order_calls}) and market sell was executed.")

    with bot.state.lock:
        assert bot.state.data.get("recovery_required") is None, "False recovery_required lock was placed!"
        assert bot.state.data.get("manual_pause") is False, "manual_pause should remain False"
        assert symbol not in bot.state.data["positions"], "Position should be closed"
        assert bot.state.data["trades"][0]["status"] == "CLOSED_MANUAL_MARKET"
        assert bot.state.data["trades"][0]["exit_order_id"] == 7001
        print(f"    PASSED: Trade settled via market sell (status=CLOSED_MANUAL_MARKET, exit_order_id=7001), NO OCO fill recorded.")

    # =========================================================================
    # Part 3: DELETE times out; GET /order raises exception -> locks recovery,
    #         and NO market sell is placed
    # =========================================================================
    print("\n[3] Executing close_position_at_market where DELETE times out, and GET /order query FAILS...")
    oco_id_3 = 77733

    class MockClientPart3:
        filters = {
            "ETHUSDT": {
                "LOT_SIZE": {"minQty": "0.001", "stepSize": "0.001"},
                "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"minNotional": "10.0"},
            }
        }

        def __init__(self):
            self.market_sell_called = False
            self.get_orderlist_calls = 0

        def account(self):
            return {
                "balances": [
                    {"asset": "USDT", "free": "970.0", "locked": "0.0"},
                    {"asset": "ETH", "free": "0.01", "locked": "0.0"},
                ]
            }

        def price(self, sym):
            return 3000.0

        def query_order(self, sym, order_id):
            return self.request("GET", "/api/v3/order", {"symbol": sym, "orderId": order_id})

        def request(self, method, endpoint, params=None, signed=False):
            if endpoint == "/api/v3/account":
                return self.account()
            if endpoint == "/api/v3/order":
                if method == "GET":
                    # Simulated network drop or 500 error while querying child leg
                    raise RuntimeError("Binance API error 500: Server error while querying child order 41")
            if endpoint == "/api/v3/orderList":
                if method == "GET":
                    self.get_orderlist_calls += 1
                    if self.get_orderlist_calls == 1:
                        # Initial pre-check: OCO is EXECUTING
                        return {
                            "orderListId": oco_id_3,
                            "symbol": symbol,
                            "listStatusType": "EXEC_STARTED",
                            "listOrderStatus": "EXECUTING",
                            "orders": [
                                {"symbol": symbol, "orderId": 41},
                                {"symbol": symbol, "orderId": 42}
                            ]
                        }
                    else:
                        # Post-cancel re-query
                        return {
                            "orderListId": oco_id_3,
                            "symbol": symbol,
                            "listStatusType": "ALL_DONE",
                            "listOrderStatus": "ALL_DONE",
                            "orders": [
                                {"symbol": symbol, "orderId": 41},
                                {"symbol": symbol, "orderId": 42}
                            ]
                        }
                elif method == "DELETE":
                    raise RuntimeError("Binance API error: SocketTimeout on DELETE")
            raise ValueError(f"Unexpected request: {method} {endpoint}")

        def order(self, params):
            self.market_sell_called = True
            return {}

    test_bot.client = MockClientPart3()

    with bot.state.lock:
        bot.state.data["positions"] = {
            symbol: {
                "symbol": symbol,
                "quantity": "0.01",
                "entry_price": 3000.0,
                "stop_price": 2900.0,
                "target_price": 3125.0,
                "entry_order_id": 300,
                "oco_order_list_id": oco_id_3,
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
            "target": 3125.0,
            "qty": "0.01",
            "order_id": 300,
            "oco_id": oco_id_3,
            "time": time.time(),
        }]
        bot.state.data["manual_pause"] = False
        bot.state.data["recovery_required"] = None
        bot.state.save()

    caught_runtime_error = False
    try:
        test_bot.close_position_at_market(symbol)
    except RuntimeError as exc:
        caught_runtime_error = True
        print(f"    Caught expected runtime error: {exc}")

    assert caught_runtime_error is True, "close_position_at_market did NOT raise RuntimeError when leg query failed!"
    assert test_bot.client.market_sell_called is False, "CRITICAL ERROR: Market sell was placed despite failed leg query!"
    print("    PASSED: Confirmed market sell was NOT placed (market_sell_called=False).")

    with bot.state.lock:
        assert bot.state.data.get("recovery_required") is not None, "recovery_required was NOT set!"
        assert bot.state.data.get("manual_pause") is True, "manual_pause was NOT set to True!"
        assert symbol in bot.state.data["positions"], "Position was improperly removed from state!"
        rec_reason = bot.state.data["recovery_required"]["reason"]
        print(f"    PASSED: recovery_required lock properly engaged: {rec_reason}")

    print("\n=== ALL 3 CANCEL TIMEOUT RESOLUTION SCENARIOS PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()
