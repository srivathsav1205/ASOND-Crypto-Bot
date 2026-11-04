#!/usr/bin/env python3
"""
initialize_allcoin_live_state.py

Fail-closed initialization script for live trading state file:
bot_state_allcoins_live.json.

Performs read-only account preflight checks against Binance Spot API:
1. Validates API credentials and base URL (https://api.binance.com).
2. Verifies account status (canTrade == True).
3. Verifies zero open orders exist on Binance Spot (fails if untracked orders exist).
4. Verifies no untracked non-USDT assets > $1.00 exist (only USDT and BNB fee assets allowed).
5. Queries baseline equity and initializes fresh state file with full safety fields.

Usage:
    python initialize_allcoin_live_state.py [--force]
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def load_dotenv():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

load_dotenv()

BASE_URL = os.getenv("BINANCE_BASE_URL", "").rstrip("/")
API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
TARGET_FILE = Path(os.getenv("BOT_STATE_FILE", str(ROOT / "bot_state_allcoins_live.json")))


def api_request(method: str, path: str, params: dict = None, signed: bool = False) -> dict | list:
    if params is None:
        params = {}
    query_str = urllib.parse.urlencode(sorted(params.items()))
    
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        query_str = urllib.parse.urlencode(sorted(params.items()))
        signature = hmac.new(
            API_SECRET.encode("utf-8"),
            query_str.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        query_str += f"&signature={signature}"

    url = f"{BASE_URL}{path}"
    if query_str:
        url += f"?{query_str}"

    headers = {"User-Agent": "AntigravityLiveInitializer/1.0"}
    if API_KEY:
        headers["X-MBX-APIKEY"] = API_KEY

    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data)
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {path}: {err_body}") from exc
    except Exception as exc:
        raise RuntimeError(f"API request failed for {path}: {exc}") from exc


def main():
    parser = argparse.ArgumentParser(description="Initialize bot_state_allcoins_live.json with live account baseline.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing bot_state_allcoins_live.json if present")
    args = parser.parse_args()

    print("=" * 70)
    print(" BINANCE SPOT LIVE TRADING INITIALIZATION PREFLIGHT")
    print("=" * 70)

    # 0. Target File Safety Check
    target_name = TARGET_FILE.name.lower()
    if "signal_only" in target_name or "paper" in target_name or "live" not in target_name:
        print(f"[!] REFUSED: initialize_allcoin_live_state.py is strictly for live mode state files.")
        print(f"    Target '{TARGET_FILE.name}' does not match expected live pattern (*live*.json).")
        sys.exit(1)

    # 1. Base URL & Credential Validation
    if BASE_URL != "https://api.binance.com":
        print(f"[!] REFUSED: BINANCE_BASE_URL must be exactly 'https://api.binance.com' for live initialization.")
        print(f"    Current value in .env is: '{BASE_URL}'")
        sys.exit(1)

    if not API_KEY or not API_SECRET:
        print("[!] REFUSED: BINANCE_API_KEY and BINANCE_API_SECRET must be configured in .env.")
        sys.exit(1)

    if TARGET_FILE.exists():
        try:
            existing = json.loads(TARGET_FILE.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                if existing.get("positions"):
                    print(f"[!] REFUSED: {TARGET_FILE.name} currently tracks active open position(s): {list(existing['positions'].keys())}.")
                    print("    Cannot re-initialize state while open positions exist! Reconcile and close positions first.")
                    sys.exit(1)
                if existing.get("recovery_required"):
                    print(f"[!] REFUSED: {TARGET_FILE.name} has active recovery_required state: {existing['recovery_required']}.")
                    print("    Cannot re-initialize state while an unresolved recovery lock is active! Even with --force, reconcile exchange state first.")
                    sys.exit(1)
        except Exception as exc:
            print(f"[!] REFUSED: {TARGET_FILE.name} already exists but could not be parsed as valid JSON: {exc}.")
            print("    Refusing to overwrite an unreadable state file, even under --force.")
            print("    Manually inspect, reconcile, and back up or remove the unreadable file first to protect untracked positions.")
            sys.exit(1)
        if not args.force:
            print(f"[!] REFUSED: {TARGET_FILE.name} already exists.")
            print("    If you are sure you want to reset flat live trading state, rerun with --force.")
            sys.exit(1)

    # 2. Connectivity & Account Status
    print("[1/5] Querying Binance Spot account status...")
    try:
        account_info = api_request("GET", "/api/v3/account", signed=True)
    except Exception as exc:
        print(f"[!] Preflight Failed: Could not connect to Binance Spot API: {exc}")
        sys.exit(1)

    if not account_info.get("canTrade", False):
        print("[!] REFUSED: Account 'canTrade' permission is False. Trading is not permitted on this API key.")
        sys.exit(1)

    # 3. Check Open Orders on Exchange
    print("[2/5] Checking account open orders across all pairs...")
    try:
        open_orders = api_request("GET", "/api/v3/openOrders", signed=True)
    except Exception as exc:
        print(f"[!] Preflight Failed: Could not query open orders: {exc}")
        sys.exit(1)

    if open_orders:
        print(f"[!] REFUSED: Account currently has {len(open_orders)} open order(s) on Binance Spot:")
        for order in open_orders[:5]:
            print(f"    - {order.get('symbol')} OrderId={order.get('orderId')} Type={order.get('type')} Side={order.get('side')} OrigQty={order.get('origQty')}")
        if len(open_orders) > 5:
            print(f"    ... and {len(open_orders) - 5} more.")
        print("\nAll open orders must be canceled or resolved before initializing a clean flat state.")
        sys.exit(1)

    print("      OK: Zero open orders detected on Binance Spot.")

    # 4. Check Balances & Untracked Assets
    print("[3/5] Inspecting balances and checking for untracked assets...")
    raw_balances = account_info.get("balances", [])
    balances = {}
    for b in raw_balances:
        free = Decimal(str(b.get("free", "0")))
        locked = Decimal(str(b.get("locked", "0")))
        if free > 0 or locked > 0:
            balances[b["asset"]] = (free, locked)

    untracked_assets = []
    bnb_equity = Decimal("0")

    for asset, (free, locked) in balances.items():
        total = free + locked
        if asset == "USDT":
            continue
        if asset == "BNB":
            try:
                price_info = api_request("GET", "/api/v3/ticker/price", {"symbol": "BNBUSDT"})
                bnb_px = Decimal(str(price_info.get("price", "0")))
                bnb_equity = total * bnb_px
            except Exception:
                pass
            continue

        # Item 6: Only skip untracked check for BNB fee assets.
        # Check approx USDT value
        approx_val = Decimal("0")
        try:
            px_info = api_request("GET", "/api/v3/ticker/price", {"symbol": f"{asset}USDT"})
            approx_val = total * Decimal(str(px_info.get("price", "0")))
        except Exception as exc:
            print(f"[!] REFUSED: Could not determine market price for non-USDT asset {asset}: {exc}.")
            print("    Valuation must fail closed. Resolve network/API access or transfer the asset before initializing.")
            sys.exit(1)

        if approx_val > Decimal("1.00"):
            untracked_assets.append((asset, float(total), float(approx_val)))

    if untracked_assets:
        print(f"[!] REFUSED: Found {len(untracked_assets)} untracked non-USDT asset(s) with value > $1.00:")
        for asset, amt, val in untracked_assets:
            print(f"    - {asset}: {amt:.4f} (~${val:.2f} USDT)")
        print("\nThe live swing bot operates exclusively on USDT capital (with BNB reserved for Spot fees).")
        print("Untracked Spot holdings must be reconciled or transferred before initializing.")
        sys.exit(1)

    print("      OK: No untracked non-USDT assets found.")

    # 5. Establish Baseline Equity (matches bot.py equity_usdt strategy capital)
    print("[4/5] Establishing baseline equity...")
    usdt_free, usdt_locked = balances.get("USDT", (Decimal("0"), Decimal("0")))
    strategy_equity = float(usdt_free + usdt_locked)

    if strategy_equity <= 0:
        print(f"[!] REFUSED: Strategy USDT equity is ${strategy_equity:.2f} USDT. Positive balance required.")
        sys.exit(1)

    print(f"      USDT Free   : ${float(usdt_free):.2f}")
    print(f"      USDT Locked : ${float(usdt_locked):.2f}")
    if bnb_equity > 0:
        print(f"      BNB Value   : ${float(bnb_equity):.2f} (fee deduction reserve only; excluded from strategy equity)")
    print(f"      Strategy Equity: ${strategy_equity:.2f} USDT")

    # 6. Generate State File with full schema matching bot.State
    print("[5/5] Writing fresh live state...")
    now = time.time()
    day_key = time.strftime("%Y-%m-%d", time.gmtime(now))
    week_key = time.strftime("%Y-W%W", time.gmtime(now))

    state_data = {
        "positions": {},
        "setups": {},
        "trades": [],
        "events": [
            {
                "time": now,
                "message": f"Initialized fresh live trading state with baseline equity ${strategy_equity:.2f} USDT."
            }
        ],
        "day_key": day_key,
        "day_start_equity": round(strategy_equity, 4),
        "week_key": week_key,
        "week_start_equity": round(strategy_equity, 4),
        "peak_equity": round(strategy_equity, 4),
        "last_account_equity_usdt": round(strategy_equity, 4),
        "net_external_flows_usdt": 0.0,
        "external_flow_ledger": [],
        "paused_until": 0,
        "manual_pause": False,
        "manual_pause_reason": None,
        "recovery_required": None,
        "account_change_requires_review": False,
        "last_hour_close": {},
        "last_15m_close": {},
        "setups_15m": {},
        "confirmed_15m_patterns": {},
    }

    tmp_target = TARGET_FILE.with_name(f"{TARGET_FILE.name}.tmp.{os.getpid()}")
    written = False
    for attempt in range(8):
        try:
            tmp_target.write_text(json.dumps(state_data, indent=2, sort_keys=True), encoding="utf-8")
            tmp_target.replace(TARGET_FILE)
            written = True
            break
        except OSError:
            time.sleep(0.04 * (1.5 ** attempt))

    if not written:
        try:
            if tmp_target.exists():
                tmp_target.unlink()
        except OSError:
            pass
        raise RuntimeError(f"Failed to atomically write live state file {TARGET_FILE} after 8 attempts. Direct-write refused.")

    print("=" * 70)
    print(" LIVE STATE INITIALIZED SUCCESSFULLY")
    print(f" State File : {TARGET_FILE.name}")
    print(f" Baseline   : ${strategy_equity:.2f} USDT")
    print(" You can now run the live trading bot once confirmed.")
    print("=" * 70)

if __name__ == "__main__":
    main()
