import os
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

SCRATCH_DIR = Path("tests")
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_fix7_live_state.json"
LOG_PATH = SCRATCH_DIR / "test_fix7.log"

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

def verify_order_sequence(call_log):
    """Asserts that call_log follows the required Binance Spot execution sequence:
    1. DELETE /api/v3/orderList (Cancel active OCO)
    2. GET /api/v3/account (Re-check unlocked free balance)
    3. POST /api/v3/order (Market SELL of verified free balance)
    Fails with AssertionError if the sequence is reversed or balance check is missing.
    """
    cancel_indices = [i for i, c in enumerate(call_log) if c[0] == "DELETE" and c[1] == "/api/v3/orderList"]
    balance_indices = [i for i, c in enumerate(call_log) if c[0] == "GET" and c[1] == "/api/v3/account"]
    sell_indices = [i for i, c in enumerate(call_log) if c[0] == "POST" and c[1] == "/api/v3/order" and c[2].get("side") == "SELL"]

    assert len(cancel_indices) >= 1, f"Missing OCO cancellation in call log: {call_log}"
    assert len(sell_indices) >= 1, f"Missing Market SELL in call log: {call_log}"

    first_cancel = cancel_indices[0]
    first_sell = sell_indices[0]

    if first_sell < first_cancel:
        raise AssertionError(
            f"REVERSED ORDER ERROR: Market SELL (call #{first_sell}) was executed BEFORE OCO cancel (call #{first_cancel})! "
            f"On Binance Spot, tokens are locked by OCO and this causes HTTP -2010 Insufficient Balance."
        )

    # Free balance verification must occur after cancel and before sell
    interim_balances = [i for i in balance_indices if first_cancel < i < first_sell]
    assert len(interim_balances) >= 1, (
        f"BALANCE VERIFICATION MISSING: No GET /api/v3/account balance check found between OCO cancel (call #{first_cancel}) "
        f"and Market SELL (call #{first_sell})!"
    )
    return True

def main():
    print("=== STARTING FIX 7 TEST: REAL LIVE CLOSE ORDER SEQUENCE ASSERTIONS ===")

    test_bot = bot.SwingBot.__new__(bot.SwingBot)
    test_bot.last_hour_close = {}

    symbol = "ETHUSDT"
    oco_id = 99901

    class MockClient:
        filters = {
            "ETHUSDT": {
                "LOT_SIZE": {"minQty": "0.001", "stepSize": "0.001"},
                "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"minNotional": "10.0"},
            }
        }

        def __init__(self):
            self.call_log = []
            self.wallet_usdt = 971.0
            self.wallet_eth_free = "0.00"
            self.wallet_eth_locked = "0.01"
            self.fail_cancel = False
            self.fail_market_sell = False
            self.simulate_missing_free_balance = False
            self.cancel_returns_filled = False
            self.cancel_returns_partial = False
            self.sell_partial_fill = False

        def account(self):
            self.call_log.append(("GET", "/api/v3/account", {}))
            return {
                "balances": [
                    {"asset": "USDT", "free": str(self.wallet_usdt), "locked": "0.0"},
                    {"asset": "ETH", "free": str(self.wallet_eth_free), "locked": str(self.wallet_eth_locked)},
                    {"asset": "BNB", "free": "0.1", "locked": "0.0"},
                ]
            }

        def price(self, sym):
            return 3000.0

        def request(self, method, endpoint, params=None, signed=False):
            self.call_log.append((method, endpoint, params or {}))
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
                    if self.fail_cancel:
                        raise RuntimeError("Binance API error: simulated network timeout during OCO cancel")
                    # On successful OCO cancel, Binance unlocks tokens into free wallet balance unless test simulates missing balance
                    if not self.simulate_missing_free_balance:
                        self.wallet_eth_free = "0.01"
                        self.wallet_eth_locked = "0.00"
                    else:
                        self.wallet_eth_free = "0.00"
                        self.wallet_eth_locked = "0.00"

                    if self.cancel_returns_filled:
                        return {
                            "orderListId": oco_id,
                            "symbol": symbol,
                            "listOrderStatus": "ALL_DONE",
                            "orders": [{"orderId": 11}, {"orderId": 12}],
                            "orderReports": [
                                {"orderId": 11, "symbol": symbol, "status": "FILLED", "executedQty": "0.01", "cummulativeQuoteQty": "31.00"},
                                {"orderId": 12, "symbol": symbol, "status": "CANCELED", "executedQty": "0.00", "cummulativeQuoteQty": "0.00"},
                            ]
                        }
                    elif self.cancel_returns_partial:
                        return {
                            "orderListId": oco_id,
                            "symbol": symbol,
                            "listOrderStatus": "ALL_DONE",
                            "orders": [{"orderId": 11}, {"orderId": 12}],
                            "orderReports": [
                                {"orderId": 11, "symbol": symbol, "status": "PARTIALLY_FILLED", "executedQty": "0.005", "cummulativeQuoteQty": "15.00"},
                                {"orderId": 12, "symbol": symbol, "status": "CANCELED", "executedQty": "0.00", "cummulativeQuoteQty": "0.00"},
                            ]
                        }
                    else:
                        return {
                            "orderListId": oco_id,
                            "symbol": symbol,
                            "listOrderStatus": "ALL_DONE",
                            "orders": [{"orderId": 11}, {"orderId": 12}],
                            "orderReports": [
                                {"orderId": 11, "symbol": symbol, "status": "CANCELED", "executedQty": "0.00", "cummulativeQuoteQty": "0.00"},
                                {"orderId": 12, "symbol": symbol, "status": "CANCELED", "executedQty": "0.00", "cummulativeQuoteQty": "0.00"},
                            ]
                        }
            raise ValueError(f"Unexpected request: {method} {endpoint}")

        def query_order(self, sym, order_id):
            self.call_log.append(("GET", f"/api/v3/order/{order_id}", {}))
            if order_id == 11:
                return {
                    "orderId": 11,
                    "symbol": symbol,
                    "orderListId": oco_id,
                    "side": "SELL",
                    "type": "LIMIT_MAKER",
                    "status": "FILLED",
                    "origQty": "0.01",
                    "executedQty": "0.01",
                    "cummulativeQuoteQty": "31.00"
                }
            elif order_id == 12:
                return {
                    "orderId": 12,
                    "symbol": symbol,
                    "orderListId": oco_id,
                    "side": "SELL",
                    "type": "STOP_LOSS_LIMIT",
                    "status": "CANCELED",
                    "origQty": "0.01",
                    "executedQty": "0.00",
                    "cummulativeQuoteQty": "0.00"
                }
            raise ValueError(f"Unknown order_id {order_id}")

        def order(self, payload):
            self.call_log.append(("POST", "/api/v3/order", payload))
            if self.fail_market_sell:
                raise RuntimeError("Binance API error: simulated network failure on market sell")
            if self.sell_partial_fill:
                return {
                    "orderId": 5001,
                    "symbol": symbol,
                    "status": "PARTIALLY_FILLED",
                    "executedQty": "0.005",
                    "cummulativeQuoteQty": "15.00"
                }
            qty = float(payload["quantity"])
            self.wallet_eth_free = "0.00"
            self.wallet_usdt += qty * 3000.0
            return {
                "orderId": 5001,
                "symbol": symbol,
                "status": "FILLED",
                "executedQty": payload["quantity"],
                "cummulativeQuoteQty": str(qty * 3000.0)
            }

    test_bot.client = MockClient()

    def reset_state():
        with bot.state.lock:
            bot.state.data["positions"] = {
                symbol: {
                    "symbol": symbol,
                    "quantity": "0.01",
                    "entry_price": 2900.0,
                    "stop_price": 2800.0,
                    "target_price": 3100.0,
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
                "entry": 2900.0,
                "stop": 2800.0,
                "target": 3100.0,
                "qty": "0.01",
                "order_id": 100,
                "oco_id": oco_id,
                "time": time.time(),
            }]
            bot.state.data["manual_pause"] = False
            bot.state.data["recovery_required"] = None
            bot.state.save()

    # =========================================================================
    # Test Scenario 1: Real Order Sequence Assertion & Reversed Order Detection
    # =========================================================================
    print("[1] Testing Happy Path Exit: Asserting real execution sequence...")
    reset_state()
    test_bot.client = MockClient()

    res = test_bot.close_position_at_market(symbol)
    assert res.get("ok") is True, f"Manual close should succeed: {res}"
    print(f"    Raw API Call Sequence: {[f'{m} {ep}' for m, ep, _ in test_bot.client.call_log]}")

    # Assert correct order
    verify_order_sequence(test_bot.client.call_log)
    print("    VERIFIED: Sequence correctly ran: DELETE /api/v3/orderList -> GET /api/v3/account -> POST /api/v3/order")

    # Assert that a simulated reversed sequence fails the sequence verifier
    reversed_simulated_log = [
        ("POST", "/api/v3/order", {"side": "SELL"}),
        ("DELETE", "/api/v3/orderList", {}),
        ("GET", "/api/v3/account", {})
    ]
    reversed_failed = False
    try:
        verify_order_sequence(reversed_simulated_log)
    except AssertionError as err:
        reversed_failed = True
        print(f"    VERIFIED: Inverted order was successfully caught and rejected: {err}")
    assert reversed_failed is True, "verify_order_sequence must fail on reversed order!"

    with bot.state.lock:
        assert symbol not in bot.state.data["positions"]
        assert bot.state.data["trades"][0]["status"] == "CLOSED_MANUAL_MARKET"
        actual_baseline = bot.state.data.get("last_account_equity_usdt")
        assert actual_baseline == 1001.0, f"Expected unmocked baseline 1001.0, got {actual_baseline}"
    print(f"    VERIFIED: Position closed cleanly and unmocked baseline re-established = {actual_baseline}")

    # =========================================================================
    # Test Scenario 2: OCO Cancel Failure (Fail Closed)
    # =========================================================================
    print("\n[2] Testing OCO Cancel Failure: Ensuring market sell is never attempted...")
    reset_state()
    test_bot.client = MockClient()
    test_bot.client.fail_cancel = True

    cancel_failed = False
    try:
        test_bot.close_position_at_market(symbol)
    except RuntimeError as e:
        cancel_failed = True
        print(f"    Caught expected cancel failure: {e}")

    assert cancel_failed is True, "close_position_at_market must raise RuntimeError when OCO cancel fails!"
    # Ensure POST /api/v3/order was NEVER invoked
    market_sells = [c for c in test_bot.client.call_log if c[0] == "POST" and c[1] == "/api/v3/order"]
    assert len(market_sells) == 0, "CRITICAL ERROR: Market sell was attempted despite OCO cancel failure!"
    print("    VERIFIED: Market sell was NOT called when OCO cancel failed.")

    with bot.state.lock:
        assert symbol in bot.state.data["positions"], "Position must remain in state when close fails"
        assert bot.state.data.get("manual_pause") is True
        assert bot.state.data.get("recovery_required") is not None
        assert "Failed to cancel OCO order list" in bot.state.data["recovery_required"]["reason"]
        print(f"    VERIFIED: Recovery lock engaged: {bot.state.data['recovery_required']['reason']}")

    # =========================================================================
    # Test Scenario 3: Free Balance Check Discrepancy After OCO Cancel
    # =========================================================================
    print("\n[3] Testing Balance Discrepancy (Coins missing from free balance after cancel)...")
    reset_state()
    test_bot.client = MockClient()
    # Simulate an exchange edge case where OCO is deleted but free balance is 0.000
    test_bot.client.simulate_missing_free_balance = True

    balance_check_failed = False
    try:
        test_bot.close_position_at_market(symbol)
    except RuntimeError as e:
        balance_check_failed = True
        print(f"    Caught expected balance failure: {e}")

    assert balance_check_failed is True
    market_sells = [c for c in test_bot.client.call_log if c[0] == "POST" and c[1] == "/api/v3/order"]
    assert len(market_sells) == 0, "Market sell must not be submitted if free balance is insufficient!"
    print("    VERIFIED: Market sell was aborted due to insufficient free balance.")
    with bot.state.lock:
        assert bot.state.data.get("recovery_required") is not None
        assert "is less than expected sell quantity" in bot.state.data["recovery_required"]["reason"]
        print(f"    VERIFIED: Recovery lock engaged: {bot.state.data['recovery_required']['reason']}")

    # =========================================================================
    # Test Scenario 4: Market Sell Failure After OCO Cancel (Coins Unlocked)
    # =========================================================================
    print("\n[4] Testing Market Sell Failure (Coins unlocked in wallet)...")
    reset_state()
    test_bot.client = MockClient()
    test_bot.client.fail_market_sell = True

    sell_failed = False
    try:
        test_bot.close_position_at_market(symbol)
    except RuntimeError as e:
        sell_failed = True
        print(f"    Caught expected sell failure: {e}")

    assert sell_failed is True
    with bot.state.lock:
        assert symbol in bot.state.data["positions"]
        assert bot.state.data.get("recovery_required") is not None
        assert "coins unlocked in wallet" in bot.state.data["recovery_required"]["reason"]
        print(f"    VERIFIED: Recovery lock accurately flagged that coins are unlocked in wallet: {bot.state.data['recovery_required']['reason']}")

    # =========================================================================
    # Test Scenario 5: Market Sell Partial Fill
    # =========================================================================
    print("\n[5] Testing Market Sell Partial Fill...")
    reset_state()
    test_bot.client = MockClient()
    test_bot.client.sell_partial_fill = True

    partial_failed = False
    try:
        test_bot.close_position_at_market(symbol)
    except RuntimeError as e:
        partial_failed = True
        print(f"    Caught expected partial fill error: {e}")

    assert partial_failed is True
    with bot.state.lock:
        assert bot.state.data.get("recovery_required") is not None
        assert "not a verified full fill" in bot.state.data["recovery_required"]["reason"]
        print(f"    VERIFIED: Recovery lock engaged for partial fill: {bot.state.data['recovery_required']['reason']}")

    # =========================================================================
    # Test Scenario 6: OCO Leg Filled Prior to Cancellation
    # =========================================================================
    print("\n[6] Testing OCO Child Leg Filled Prior to Cancellation...")
    reset_state()
    test_bot.client = MockClient()
    test_bot.client.cancel_returns_filled = True

    res6 = test_bot.close_position_at_market(symbol)
    assert res6.get("ok") is True
    market_sells = [c for c in test_bot.client.call_log if c[0] == "POST" and c[1] == "/api/v3/order"]
    assert len(market_sells) == 0, "No market sell should be executed if an OCO leg already filled!"
    print("    VERIFIED: No redundant market sell placed when OCO child leg already filled.")

    with bot.state.lock:
        assert symbol not in bot.state.data["positions"]
        assert bot.state.data["trades"][0]["status"] == "CLOSED_OCO_TARGET"
        print(f"    VERIFIED: Position settled via OCO fill logic (trade status = {bot.state.data['trades'][0]['status']})")

    print("\n=== ALL FIX 7 SCENARIOS PASSED WITH PERFECT EXECUTION INTEGRITY ===")

if __name__ == "__main__":
    main()
