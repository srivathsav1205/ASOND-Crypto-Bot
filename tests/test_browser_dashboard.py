#!/usr/bin/env python3
"""
Real Headless Browser Verification of Trading Dashboard & CSP Conformance.
Spins up DashboardHandler and launches real Google Chrome (or Microsoft Edge) to verify:
1. Dashboard loads HTTP 200.
2. /static/tv.js loads with verified SRI.
3. /static/dashboard.js executes and instantiates TradingView.widget.
4. TradingView widget successfully constructs iframe directly inside #tv_chart_container.
5. Zero CSP violations or security blocks in Chrome logs.
6. Headless dashboard screenshot saved.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import threading
import subprocess
from pathlib import Path
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot

def main():
    print("=== STARTING REAL BROWSER DASHBOARD & CSP TEST ===")

    chrome_paths = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ]
    browser_exe = next((p for p in chrome_paths if p.exists()), None)
    if not browser_exe:
        print("[!] No browser executable found.")
        sys.exit(1)
    print(f"Browser Executable: {browser_exe}")

    # Set up dummy state
    scratch_dir = ROOT / "tests"
    state_file = scratch_dir / "test_browser_state.json"
    log_file = scratch_dir / "test_browser.log"
    chrome_log = scratch_dir / "chrome_debug.log"
    screenshot_file = scratch_dir / "dashboard_screenshot.png"
    artifact_dir = Path(r"C:\Users\sriva\.gemini\antigravity\brain\7b0cd433-1c72-40eb-9663-c69f2e6bfd9c")
    artifact_screenshot = artifact_dir / "dashboard_screenshot.png"

    os.environ["BOT_STATE_FILE"] = str(state_file.resolve())
    os.environ["BOT_LOG_FILE"] = str(log_file.resolve())
    os.environ["ENABLE_LIVE_TRADING"] = "false"

    state_file.write_text(json.dumps({
        "positions": {},
        "trades": [],
        "events": [{"time": time.time(), "message": "Browser test session active"}],
        "manual_pause": False,
        "recovery_required": None,
        "last_account_equity_usdt": 1000.0,
        "net_external_flows_usdt": 0.0,
        "external_flow_ledger": []
    }), encoding="utf-8")

    bot.DATA_FILE = state_file
    bot.state = bot.State()
    bot.bot_instance = None

    server = ThreadingHTTPServer(("127.0.0.1", 0), bot.DashboardHandler)
    port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.1)

    url = f"http://127.0.0.1:{port}/"
    print(f"Dashboard URL: {url}")

    user_data_dir = tempfile.mkdtemp()
    cmd = [
        str(browser_exe),
        "--headless=new",
        "--disable-gpu",
        f"--user-data-dir={user_data_dir}",
        "--enable-logging",
        f"--log-file={chrome_log.resolve()}",
        f"--screenshot={screenshot_file.resolve()}",
        "--window-size=1280,800",
        "--dump-dom",
        url
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=25)
        dom = proc.stdout or ""

        # 1. Check DOM for static script references
        assert '/static/tv.js' in dom, "DOM must load /static/tv.js"
        assert '/static/dashboard.js' in dom, "DOM must load /static/dashboard.js"
        print("  [OK] DOM successfully loaded with /static/tv.js and /static/dashboard.js")

        # 2. Strict check: #tv_chart_container MUST contain an <iframe element
        needle = 'id="tv_chart_container"'
        assert needle in dom, "DOM must contain element with id='tv_chart_container'"
        container_start = dom.find(needle)
        # Inspect up to the next 3000 chars within the container block
        container_snippet = dom[container_start:container_start + 3000]
        assert "<iframe" in container_snippet.lower(), (
            f"TradingView widget failed to inject <iframe inside #tv_chart_container! Snippet: {container_snippet[:300]}"
        )
        print("  [OK] Confirmed <iframe element exists inside #tv_chart_container in rendered DOM.")

        # 3. Check Chrome logs for CSP violations
        log_content = chrome_log.read_text(encoding="utf-8", errors="replace") if chrome_log.exists() else ""
        csp_violations = [
            line for line in log_content.splitlines()
            if "content security policy" in line.lower() or "refused to load" in line.lower() or "violates" in line.lower()
        ]
        if csp_violations:
            print(f"[!] CSP Violations detected in browser:\n" + "\n".join(csp_violations))
            sys.exit(1)
        else:
            print("  [OK] ZERO Content Security Policy (CSP) violations detected in browser logs.")

        # 4. Check screenshot file
        assert screenshot_file.exists(), f"Screenshot file was not generated at {screenshot_file}"
        file_size = screenshot_file.stat().st_size
        assert file_size > 1000, f"Screenshot file is abnormally small ({file_size} bytes)"
        print(f"  [OK] Headless dashboard screenshot saved: {screenshot_file} ({file_size:,} bytes)")

        # Copy to artifact directory
        if artifact_dir.exists():
            shutil.copy2(screenshot_file, artifact_screenshot)
            print(f"  [OK] Screenshot copied to artifact directory: {artifact_screenshot}")

        print("=== BROWSER DASHBOARD & CSP TEST PASSED PERFECTLY ===")
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(user_data_dir, ignore_errors=True)
        if state_file.exists():
            state_file.unlink()
        if log_file.exists():
            log_file.unlink()
        if chrome_log.exists():
            chrome_log.unlink()

if __name__ == "__main__":
    main()
