#!/usr/bin/env python3
"""
record_external_flow.py

CLI tool to record manual external flows (USDT deposits or withdrawals)
in the bot's persisted state.

This ensures the unrecorded account equity change monitor (check_unrecorded_flat_account_change)
factors out external deposits/withdrawals, preventing false win-locks or deposit-locks.

Usage:
    python record_external_flow.py --amount 50.0 --reason "deposit from bank"
    python record_external_flow.py --amount -20.0 --reason "withdrawal"
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def determine_mode() -> bool:
    env_file = Path(os.environ.get("ENV_FILE", ROOT / ".env"))
    if not env_file.exists():
        print(f"[!] Refused: .env file not found at {env_file}. Mode undetermined.")
        sys.exit(1)

    is_live = None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        clean_line = line.split("#", 1)[0].strip()
        m = re.match(r"^ENABLE_LIVE_TRADING\s*=\s*(.+)$", clean_line, re.IGNORECASE)
        if m:
            raw_val = m.group(1).strip().strip("'\"").lower()
            if raw_val in ("true", "1", "yes"):
                is_live = True
            elif raw_val in ("false", "0", "no"):
                is_live = False
            else:
                print(f"[!] Refused: Invalid ENABLE_LIVE_TRADING value in .env: '{raw_val}'. Mode undetermined.")
                sys.exit(1)
            break

    if is_live is None:
        print("[!] Refused: ENABLE_LIVE_TRADING not set or undetermined in .env. Never falling back to paper silently.")
        sys.exit(1)
    return is_live


def find_state_file(explicit_path=None) -> Path:
    is_live = determine_mode()

    if explicit_path:
        p = Path(explicit_path)
        if not p.is_absolute():
            p = ROOT / p
        target = p
    elif os.environ.get("BOT_STATE_FILE"):
        target = Path(os.environ["BOT_STATE_FILE"]).resolve()
    else:
        live_path = ROOT / "bot_state_allcoins_live.json"
        paper_path = ROOT / "bot_state_signal_only.json"
        target = live_path if is_live else paper_path

    # Guard: verify filename matches mode
    target_name = target.name.lower()
    if is_live:
        if "paper" in target_name or "signal_only" in target_name or "live" not in target_name:
            print(f"[!] Refused: Target state file '{target.name}' does not match live mode (expected *live*.json).")
            sys.exit(1)
    else:
        if "live" in target_name and ("paper" not in target_name and "signal_only" not in target_name):
            print(f"[!] Refused: Target state file '{target.name}' does not match paper mode (expected *signal_only*.json or *paper*.json).")
            sys.exit(1)

    return target


def main():
    parser = argparse.ArgumentParser(description="Record an external deposit/withdrawal in bot state.")
    parser.add_argument("--amount", type=float, required=True, help="Flow amount in USDT (+ for deposit, - for withdrawal)")
    parser.add_argument("--reason", type=str, default="manual external flow", help="Description / reason for the flow")
    parser.add_argument("--state-file", type=str, default=None, help="Explicit path to bot state JSON file")
    parser.add_argument("--confirm", "--yes", "-y", dest="confirmed", action="store_true", help="Bypass interactive confirmation prompt")

    args = parser.parse_args()
    amount = args.amount
    reason = args.reason

    if amount == 0:
        print("[!] Flow amount cannot be zero.")
        sys.exit(1)
    if abs(amount) > 1_000_000:
        print(f"[!] Refused: Flow amount ({amount:+.2f} USDT) exceeds sanity limit ($1,000,000).")
        sys.exit(1)

    state_file = find_state_file(args.state_file)
    if not state_file.exists():
        print(f"[!] State file not found: {state_file}")
        print("    Initialize state first before recording external flows.")
        sys.exit(1)

    if not args.confirmed:
        flow_type = "DEPOSIT" if amount > 0 else "WITHDRAWAL"
        print("CONFIRMATION REQUIRED:")
        print(f"  Target File : {state_file.name}")
        print(f"  Flow Type   : {flow_type}")
        print(f"  Amount      : {amount:+.4f} USDT")
        print(f"  Reason      : {reason}")
        if sys.stdin.isatty():
            ans = input(f"Type 'YES' to confirm this {flow_type}: ").strip()
            if ans != "YES":
                print("[!] Confirmation failed. Aborting.")
                sys.exit(1)
        else:
            print("[!] Non-interactive shell requires --confirm / -y flag to record external flows.")
            sys.exit(1)

    # Read state
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("State root must be a JSON object")
    except Exception as exc:
        print(f"[!] Failed to read {state_file.name}: {exc}")
        sys.exit(1)

    now = time.time()
    current_net = float(data.get("net_external_flows_usdt", 0) or 0)
    new_net = round(current_net + amount, 4)
    data["net_external_flows_usdt"] = new_net

    # Ledger entry - Item 9: Single state key external_flow_ledger everywhere
    flow_entry = {
        "time": now,
        "amount": round(amount, 4),
        "reason": reason,
        "net_flows_after": new_net
    }
    # Migrate any legacy external_flows list to external_flow_ledger
    ledger = data.get("external_flow_ledger")
    if ledger is None:
        ledger = data.get("external_flows", [])
    if not isinstance(ledger, list):
        ledger = []
    ledger.append(flow_entry)
    data["external_flow_ledger"] = ledger
    data.pop("external_flows", None)

    # Log event
    event_msg = f"External flow recorded: {amount:+.2f} USDT ({reason}). Net flows: {new_net:+.2f} USDT."
    data.setdefault("events", []).append({
        "time": now,
        "message": event_msg
    })
    data["events"] = data["events"][-200:]

    # Auto-resolve unrecorded change pause if present
    # Item 4: Only clear unrecorded_account_change locks. NEVER clear recovery_required!
    cleared_pause = False
    if data.get("account_change_requires_review"):
        data["account_change_requires_review"] = False
        cleared_pause = True

    if data.get("manual_pause_reason") == "unrecorded_account_change":
        data["manual_pause"] = False
        data["manual_pause_reason"] = None
        cleared_pause = True

    # Atomic write with Windows retry and unique PID temp file
    payload = json.dumps(data, indent=2, sort_keys=True, allow_nan=False)
    tmp_file = state_file.with_name(f"{state_file.name}.tmp.{os.getpid()}")
    written = False
    for attempt in range(8):
        try:
            tmp_file.write_text(payload, encoding="utf-8")
            tmp_file.replace(state_file)
            written = True
            break
        except OSError:
            time.sleep(0.04 * (1.5 ** attempt))

    if not written:
        try:
            if tmp_file.exists():
                tmp_file.unlink()
        except OSError:
            pass
        print(f"[!] Critical: Atomic state save (replace) failed after 8 retries. Previous state file preserved.")
        sys.exit(1)

    print("=" * 60)
    print(" EXTERNAL FLOW RECORDED SUCCESSFULLY")
    print("=" * 60)
    print(f" State File       : {state_file.name}")
    print(f" Flow Amount      : {amount:+.2f} USDT")
    print(f" Reason           : {reason}")
    print(f" Prior Net Flows  : {current_net:+.2f} USDT")
    print(f" Updated Net Flows: {new_net:+.2f} USDT")
    if cleared_pause:
        print(" Action           : Cleared unrecorded_account_change lock and resumed entries.")
    print("=" * 60)

if __name__ == "__main__":
    main()
