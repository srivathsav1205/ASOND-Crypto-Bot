import os
import sys
import time
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

# Enforce isolated test state and log files
SCRATCH_DIR = Path("tests")
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_fix2_state.json"
LOG_PATH = SCRATCH_DIR / "test_fix2.log"

if STATE_PATH.exists():
    STATE_PATH.unlink()
if LOG_PATH.exists():
    LOG_PATH.unlink()

os.environ["BOT_STATE_FILE"] = str(STATE_PATH.resolve())
os.environ["BOT_LOG_FILE"] = str(LOG_PATH.resolve())
os.environ["ENABLE_LIVE_TRADING"] = "false"

import bot

def main():
    print("=== STARTING FIX 2 TEST ===")
    
    # Instantiate bot without network
    test_bot = bot.SwingBot.__new__(bot.SwingBot)
    test_bot.last_hour_close = {}
    
    # Mock client filters & price
    class MockClient:
        filters = {
            "ETHUSDT": {
                "LOT_SIZE": {"minQty": "0.001", "stepSize": "0.001"},
                "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"minNotional": "10.0"},
            }
        }
        def price(self, symbol):
            return 3000.0

    test_bot.client = MockClient()

    # Reset state data
    with bot.state.lock:
        bot.state.data["positions"] = {}
        bot.state.data["trades"] = []
        bot.state.data["manual_pause"] = False
        bot.state.data["recovery_required"] = None
        bot.state.data["net_external_flows_usdt"] = 0.0
        bot.state.data["last_account_equity_usdt"] = 1000.0
        bot.state.save()

    sig = {
        "symbol": "ETHUSDT",
        "entry": 3000.0,
        "stop": 2940.0,
        "target": 3120.0,
        "level": 3000.0,
        "score": 85,
        "score_desc": "Score 85/100",
    }
    equity = 1000.0
    balances = {"USDT": (1000.0, 0.0)}

    # Step 1: Open paper trade
    print("[1] Opening first trade for ETHUSDT...")
    test_bot.open_trade(sig, equity, balances)

    with bot.state.lock:
        positions = bot.state.data["positions"]
        trades = bot.state.data["trades"]

    assert "ETHUSDT" in positions, "ETHUSDT should be in positions"
    assert len(trades) == 1, f"Expected 1 trade, got {len(trades)}"
    trade1 = trades[0]
    trade1_id = trade1["id"]
    assert trade1["status"] == "OPEN", f"Expected OPEN, got {trade1['status']}"
    assert trade1["id"].startswith("paper_ETHUSDT_"), f"Unexpected trade id format: {trade1['id']}"
    assert positions["ETHUSDT"]["trade_id"] == trade1_id
    print(f"    Trade 1 opened successfully with trade_id: {trade1_id}")

    # Step 2: Try calling open_trade again while trade is OPEN
    print("[2] Attempting to open duplicate trade while OPEN...")
    test_bot.open_trade(sig, equity, balances)

    with bot.state.lock:
        trades_after_dup = bot.state.data["trades"]
        positions_after_dup = bot.state.data["positions"]

    assert len(trades_after_dup) == 1, f"Duplicate trade was opened! Trade count: {len(trades_after_dup)}"
    assert len(positions_after_dup) == 1, f"Positions count changed: {len(positions_after_dup)}"
    print("    Duplicate open_trade correctly refused; trade count remains 1.")

    # Step 3: Call settle function first time
    print("[3] Settle paper position first time...")
    settled1 = test_bot._settle_paper_position("ETHUSDT", "CLOSED_TARGET", 3120.0, exit_reason="TARGET_HIT")
    assert settled1 is not None, "First settle call should return settled trade dict"
    assert settled1["id"] == trade1_id, f"Expected trade_id {trade1_id}, got {settled1['id']}"
    assert settled1["status"] == "CLOSED_TARGET", f"Expected CLOSED_TARGET, got {settled1['status']}"

    with bot.state.lock:
        assert "ETHUSDT" not in bot.state.data["positions"], "Position was not removed from positions"
        assert len(bot.state.data["trades"]) == 1, "Trades count should remain 1"
        assert bot.state.data["trades"][0]["status"] == "CLOSED_TARGET"
        pnl1 = bot.state.data["trades"][0]["pnl"]
    print(f"    Settlement 1 succeeded: trade_id={trade1_id}, status={settled1['status']}, pnl={pnl1}")

    # Step 4: Call settle function second time (idempotency check)
    print("[4] Settle paper position second time (duplicate call)...")
    settled2 = test_bot._settle_paper_position("ETHUSDT", "CLOSED_TARGET", 3120.0, exit_reason="TARGET_HIT")
    assert settled2 is None, f"Second settle call should return None, got {settled2}"

    with bot.state.lock:
        assert len(bot.state.data["trades"]) == 1, f"Expected 1 trade after duplicate settle, got {len(bot.state.data['trades'])}"
        assert bot.state.data["trades"][0]["status"] == "CLOSED_TARGET"
        assert "ETHUSDT" not in bot.state.data["positions"]
    print("    Duplicate settlement correctly returned None; state and trade records remain unchanged.")

    # Step 5: Call open after close; expect a new trade now that no open trade exists
    print("[5] Call open_trade after close...")
    # Small sleep to ensure unique timestamp in trade_id
    time.sleep(0.01)
    test_bot.open_trade(sig, equity, balances)

    with bot.state.lock:
        trades_after_reopen = bot.state.data["trades"]
        positions_after_reopen = bot.state.data["positions"]

    assert len(trades_after_reopen) == 2, f"Expected 2 trades, got {len(trades_after_reopen)}"
    assert "ETHUSDT" in positions_after_reopen, "ETHUSDT should be in positions after re-open"
    trade2 = trades_after_reopen[1]
    trade2_id = trade2["id"]
    assert trade2["status"] == "OPEN"
    assert trade2_id != trade1_id, f"Trade IDs must be unique! trade1={trade1_id}, trade2={trade2_id}"
    print(f"    New trade opened successfully with distinct trade_id: {trade2_id}")

    print("=== FIX 2 TEST PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()
