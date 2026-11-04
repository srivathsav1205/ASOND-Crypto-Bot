import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot

def main():
    print("=== STARTING TESTNET STATE SEPARATION & MODE GUARD TEST ===")

    # 1. Testnet pointed to live file -> MUST FAIL
    print("[1] Testing Testnet URL with live state file...")
    failed1 = False
    try:
        bot._validate_state_file_mode(Path("bot_state_allcoins_live.json"), is_live=True, base_url="https://testnet.binance.vision")
    except RuntimeError as e:
        failed1 = True
        print(f"    PASSED: Refused as expected: {e}")
    assert failed1 is True, "Testnet URL must refuse bot_state_allcoins_live.json"

    # 2. Testnet pointed to paper file -> MUST FAIL
    print("[2] Testing Testnet URL with paper state file...")
    failed2 = False
    try:
        bot._validate_state_file_mode(Path("bot_state_signal_only.json"), is_live=False, base_url="https://testnet.binance.vision")
    except RuntimeError as e:
        failed2 = True
        print(f"    PASSED: Refused as expected: {e}")
    assert failed2 is True, "Testnet URL must refuse bot_state_signal_only.json"

    # 3. Live URL with testnet state file -> MUST FAIL
    print("[3] Testing Live URL with testnet state file...")
    failed3 = False
    try:
        bot._validate_state_file_mode(Path("bot_state_testnet.json"), is_live=True, base_url="https://api.binance.com")
    except RuntimeError as e:
        failed3 = True
        print(f"    PASSED: Refused as expected: {e}")
    assert failed3 is True, "Production live URL must refuse testnet state file"

    # 4. Paper mode with testnet state file -> MUST FAIL
    print("[4] Testing Paper mode with testnet state file...")
    failed4 = False
    try:
        bot._validate_state_file_mode(Path("bot_state_testnet.json"), is_live=False, base_url="https://data-api.binance.vision")
    except RuntimeError as e:
        failed4 = True
        print(f"    PASSED: Refused as expected: {e}")
    assert failed4 is True, "Paper mode must refuse testnet state file"

    # 5. Testnet URL with proper testnet state file -> MUST SUCCEED
    print("[5] Testing Testnet URL with valid testnet state file...")
    bot._validate_state_file_mode(Path("bot_state_testnet.json"), is_live=True, base_url="https://testnet.binance.vision")
    bot._validate_state_file_mode(Path("test_custom_testnet.json"), is_live=True, base_url="https://testnet.binance.vision")
    print("    PASSED: Valid testnet state files accepted cleanly.")

    print("=== TESTNET STATE SEPARATION TEST PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()
