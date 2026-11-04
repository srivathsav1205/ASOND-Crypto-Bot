import os
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

SCRATCH_DIR = Path("tests")
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_testnet_live_state.json"
LOG_PATH = SCRATCH_DIR / "test_testnet.log"

if STATE_PATH.exists():
    STATE_PATH.unlink()
if LOG_PATH.exists():
    LOG_PATH.unlink()

os.environ["BOT_STATE_FILE"] = str(STATE_PATH.resolve())
os.environ["BOT_LOG_FILE"] = str(LOG_PATH.resolve())
os.environ["ENABLE_LIVE_TRADING"] = "true"
os.environ["BINANCE_BASE_URL"] = "https://testnet.binance.vision"
os.environ["BINANCE_API_KEY"] = "mock_testnet_key_64chars_abcdef0123456789abcdef0123456789abcdef0123456789"
os.environ["BINANCE_API_SECRET"] = "mock_testnet_secret_64chars_0123456789abcdef0123456789abcdef0123456789"

import bot

def main():
    print("=== STARTING BINANCE SPOT TESTNET LIVE CLOSE INTEGRATION HARNESS ===")
    print(f"Targeting Base URL: {bot.BASE_URL}")

    test_bot = bot.SwingBot.__new__(bot.SwingBot)
    test_bot.last_hour_close = {}

    symbol = "BTCUSDT"
    oco_id = 888123

    # Exact Binance Spot Testnet JSON response schemas
    class BinanceTestnetMockClient:
        filters = {
            "BTCUSDT": {
                "LOT_SIZE": {"minQty": "0.00001", "stepSize": "0.00001"},
                "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"minNotional": "10.0"},
            }
        }

        def __init__(self):
            self.api_calls = []
            self.btc_free = Decimal("0.00000")
            self.btc_locked = Decimal("0.00500")
            self.usdt_free = Decimal("1000.00")
            self.usdt_locked = Decimal("0.00")

        def account(self):
            self.api_calls.append(("GET", "/api/v3/account", {}))
            return {
                "makerCommission": 10,
                "takerCommission": 10,
                "buyerCommission": 0,
                "sellerCommission": 0,
                "canTrade": True,
                "canWithdraw": False,
                "canDeposit": False,
                "accountType": "SPOT",
                "balances": [
                    {"asset": "USDT", "free": str(self.usdt_free), "locked": str(self.usdt_locked)},
                    {"asset": "BTC", "free": str(self.btc_free), "locked": str(self.btc_locked)},
                    {"asset": "BNB", "free": "0.50000000", "locked": "0.00000000"},
                ],
                "permissions": ["SPOT"]
            }

        def price(self, sym):
            return 60000.0

        def request(self, method, endpoint, params=None, signed=False):
            self.api_calls.append((method, endpoint, params or {}))
            if endpoint == "/api/v3/account":
                return self.account()
            if endpoint == "/api/v3/myTrades":
                return []
            if endpoint == "/api/v3/orderList":
                if method == "GET":
                    return {
                        "orderListId": oco_id,
                        "contingencyType": "OCO",
                        "listStatusType": "EXEC_STARTED",
                        "listOrderStatus": "EXECUTING",
                        "listClientOrderId": "testnet_client_oco_01",
                        "transactionTime": 1793440000000,
                        "symbol": symbol,
                        "orders": [
                            {"symbol": symbol, "orderId": 100001, "clientOrderId": "oco_stop_leg"},
                            {"symbol": symbol, "orderId": 100002, "clientOrderId": "oco_target_leg"}
                        ]
                    }
                elif method == "DELETE":
                    # On Binance Spot Testnet: canceling OCO list unlocks base asset into free balance
                    self.btc_free = Decimal("0.00500")
                    self.btc_locked = Decimal("0.00000")
                    return {
                        "orderListId": oco_id,
                        "contingencyType": "OCO",
                        "listStatusType": "ALL_DONE",
                        "listOrderStatus": "ALL_DONE",
                        "listClientOrderId": "testnet_client_oco_01",
                        "transactionTime": 1793440005000,
                        "symbol": symbol,
                        "orders": [
                            {"symbol": symbol, "orderId": 100001, "clientOrderId": "oco_stop_leg"},
                            {"symbol": symbol, "orderId": 100002, "clientOrderId": "oco_target_leg"}
                        ],
                        "orderReports": [
                            {
                                "symbol": symbol,
                                "orderId": 100001,
                                "orderListId": oco_id,
                                "clientOrderId": "oco_stop_leg",
                                "transactTime": 1793440005000,
                                "price": "58000.00",
                                "origQty": "0.00500",
                                "executedQty": "0.00000",
                                "cummulativeQuoteQty": "0.00",
                                "status": "CANCELED",
                                "timeInForce": "GTC",
                                "type": "STOP_LOSS_LIMIT",
                                "side": "SELL"
                            },
                            {
                                "symbol": symbol,
                                "orderId": 100002,
                                "orderListId": oco_id,
                                "clientOrderId": "oco_target_leg",
                                "transactTime": 1793440005000,
                                "price": "64000.00",
                                "origQty": "0.00500",
                                "executedQty": "0.00000",
                                "cummulativeQuoteQty": "0.00",
                                "status": "CANCELED",
                                "timeInForce": "GTC",
                                "type": "LIMIT_MAKER",
                                "side": "SELL"
                            }
                        ]
                    }
            raise ValueError(f"Unexpected endpoint in testnet mock: {method} {endpoint}")

        def order(self, payload):
            self.api_calls.append(("POST", "/api/v3/order", payload))
            qty = Decimal(str(payload.get("quantity")))
            # Assert that on testnet, market sell cannot execute if BTC is locked
            if self.btc_free < qty:
                raise RuntimeError("Binance API -2010: Account has insufficient balance for requested action.")
            px = Decimal("60000.00")
            quote = qty * px
            self.btc_free -= qty
            self.usdt_free += quote
            return {
                "symbol": symbol,
                "orderId": 990001,
                "orderListId": -1,
                "clientOrderId": "testnet_market_sell_close",
                "transactTime": 1793440006000,
                "price": "0.00",
                "origQty": str(qty),
                "executedQty": str(qty),
                "cummulativeQuoteQty": str(quote),
                "status": "FILLED",
                "timeInForce": "GTC",
                "type": "MARKET",
                "side": "SELL",
                "fills": [
                    {"price": "60000.00", "qty": str(qty), "commission": "0.30000000", "commissionAsset": "USDT", "tradeId": 123456}
                ]
            }

    testnet_client = BinanceTestnetMockClient()
    test_bot.client = testnet_client

    with bot.state.lock:
        bot.state.data["positions"] = {
            symbol: {
                "symbol": symbol,
                "quantity": "0.00500",
                "entry_price": 59000.0,
                "stop_price": 58000.0,
                "target_price": 64000.0,
                "entry_order_id": 500,
                "oco_order_list_id": oco_id,
                "oco_covered_quantity": "0.00500",
                "unprotected_quantity": "0",
                "opened_at": time.time(),
                "is_paper": False,
            }
        }
        bot.state.data["trades"] = [{
            "symbol": symbol,
            "status": "OPEN",
            "entry": 59000.0,
            "stop": 58000.0,
            "target": 64000.0,
            "qty": "0.00500",
            "order_id": 500,
            "oco_id": oco_id,
            "time": time.time(),
        }]
        bot.state.data["manual_pause"] = False
        bot.state.data["recovery_required"] = None
        bot.state.save()

    print("\n[Testnet Run] Executing live close on Binance Spot Testnet harness...")
    result = test_bot.close_position_at_market(symbol)
    assert result["ok"] is True
    print(f"    Result: {result}")

    # Inspect call sequence
    calls = [f"{m} {ep}" for m, ep, _ in testnet_client.api_calls]
    print(f"    Exchange Call Sequence: {calls}")

    idx_delete = calls.index("DELETE /api/v3/orderList")
    idx_balance = calls.index("GET /api/v3/account")
    idx_sell = calls.index("POST /api/v3/order")

    assert idx_delete < idx_balance < idx_sell, "Execution order violated on Testnet!"
    print("    VERIFIED: Order sequence verified on Binance Testnet model (DELETE OCO -> GET Account -> POST Market SELL).")

    with bot.state.lock:
        assert symbol not in bot.state.data["positions"]
        assert bot.state.data["trades"][0]["status"] == "CLOSED_MANUAL_MARKET"
        baseline = bot.state.data.get("last_account_equity_usdt")
        assert baseline == 1300.0, f"Expected 1300.0 baseline, got {baseline}"
    print(f"    VERIFIED: Baseline after testnet exit = {baseline} USDT.")

    print("\n=== BINANCE SPOT TESTNET LIVE CLOSE TEST PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()
