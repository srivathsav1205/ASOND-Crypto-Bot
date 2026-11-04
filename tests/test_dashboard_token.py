import http.client
import json
import os
import sys
import threading
import time
from http.server import HTTPServer, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SCRATCH_DIR = PROJECT_ROOT / "tests"
SCRATCH_DIR.mkdir(exist_ok=True)

STATE_PATH = SCRATCH_DIR / "test_dash_state.json"
LOG_PATH = SCRATCH_DIR / "test_dash.log"

if STATE_PATH.exists():
    STATE_PATH.unlink()
if LOG_PATH.exists():
    LOG_PATH.unlink()

os.environ["BOT_STATE_FILE"] = str(STATE_PATH.resolve())
os.environ["BOT_LOG_FILE"] = str(LOG_PATH.resolve())
os.environ["ENABLE_LIVE_TRADING"] = "false"

import bot

def main():
    print("=== STARTING DASHBOARD TOKEN TEST (/close-position) ===")

    # Initialize dummy state
    initial_state = {
        "positions": {
            "ETHUSDT": {"symbol": "ETHUSDT", "quantity": "0.01", "entry_price": 3000.0, "opened_at": time.time(), "is_paper": True}
        },
        "trades": [{"symbol": "ETHUSDT", "status": "OPEN", "entry": 3000.0, "qty": "0.01", "order_id": 1}],
        "events": [],
        "manual_pause": False,
        "manual_pause_reason": None,
        "recovery_required": None,
        "last_account_equity_usdt": 1000.0,
        "net_external_flows_usdt": 0.0,
        "external_flow_ledger": []
    }
    STATE_PATH.write_text(json.dumps(initial_state, indent=2), encoding="utf-8")
    bot.DATA_FILE = STATE_PATH
    bot.state = bot.State()

    # Mock bot_instance
    class MockBot:
        def close_position_at_market(self, sym):
            return {"ok": True, "symbol": sym, "closed": True}
    bot.bot_instance = MockBot()

    # Pick an available port for test server (port 0 = OS assigns free ephemeral port)
    server = ThreadingHTTPServer(("127.0.0.1", 0), bot.DashboardHandler)
    test_port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.1)

    def send_post(token=None, symbol="ETHUSDT"):
        conn = http.client.HTTPConnection("127.0.0.1", test_port, timeout=5)
        headers = {
            "Content-Type": "application/json",
            "Host": f"127.0.0.1:{test_port}",
        }
        if token:
            headers["X-Dashboard-Token"] = token
        body = json.dumps({"symbol": symbol})
        conn.request("POST", "/close-position", body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, resp.reason, data

    try:
        # Test 1: Request with NO token -> 403 Forbidden
        print("[1] Sending POST /close-position with NO token...")
        status, reason, data = send_post(token=None)
        assert status == 403, f"Expected 403 without token, got {status}"
        print(f"    PASSED: Request without token returned 403 Forbidden ({reason}).")

        # Test 2: Request with WRONG token -> 403 Forbidden
        print("[2] Sending POST /close-position with WRONG token...")
        status, reason, data = send_post(token="invalid_token_12345")
        assert status == 403, f"Expected 403 with wrong token, got {status}"
        print(f"    PASSED: Request with invalid token returned 403 Forbidden ({reason}).")

        # Test 3: Request with CORRECT token -> 200 OK
        print("[3] Sending POST /close-position with VALID token...")
        status, reason, data = send_post(token=bot.DASHBOARD_TOKEN)
        assert status == 200, f"Expected 200 with valid token, got {status} ({data.decode()})"
        payload = json.loads(data.decode())
        assert payload.get("ok") is True, f"Expected ok: True, got {payload}"
        print(f"    PASSED: Request with valid token succeeded: {payload}")

        # Helper for GET requests
        def send_get(path):
            conn = http.client.HTTPConnection("127.0.0.1", test_port, timeout=5)
            headers = {"Host": f"127.0.0.1:{test_port}"}
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            headers_dict = {k.lower(): v for k, v in resp.getheaders()}
            conn.close()
            return resp.status, resp.reason, headers_dict, data

        # Test 4: GET / serves HTML with hardened CSP
        print("[4] Checking GET / dashboard HTML and CSP hardening...")
        status, reason, headers, data = send_get("/")
        assert status == 200, f"Expected 200 for /, got {status}"
        html_str = data.decode("utf-8")
        assert "Content-Security-Policy" in html_str, "CSP meta tag missing from HTML"
        assert "script-src 'self' https://s3.tradingview.com" in html_str, "Hardened script-src missing from CSP"
        assert "'unsafe-inline'" not in html_str.split("script-src")[1].split(";")[0], "script-src must not contain 'unsafe-inline'"
        assert "'unsafe-eval'" not in html_str.split("script-src")[1].split(";")[0], "script-src must not contain 'unsafe-eval'"
        assert '/static/tv.js' in html_str, "HTML must reference /static/tv.js"
        assert '/static/dashboard.js' in html_str, "HTML must reference /static/dashboard.js"
        print("    PASSED: Dashboard HTML served with strict CSP (no unsafe-inline/unsafe-eval in script-src).")

        # Test 5: GET /static/tv.js returns vendored TradingView library
        print("[5] Checking GET /static/tv.js serving & SRI verification...")
        status, reason, headers, data = send_get("/static/tv.js")
        assert status == 200, f"Expected 200 for /static/tv.js, got {status}"
        assert "application/javascript" in headers.get("content-type", "").lower()
        import hashlib, base64
        computed_sri = "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode()
        expected_sri = "sha384-PrAUhlnhshqe64CQsL37sz1scuWsYLu/6j5GpdHGyGDqL+A47QxmNl0GX9jiUEUO"
        assert computed_sri == expected_sri, f"SRI mismatch: expected {expected_sri}, got {computed_sri}"
        print(f"    PASSED: /static/tv.js served (HTTP 200, Content-Type: {headers.get('content-type')}, verified SRI: {computed_sri}).")

        # Test 6: GET /static/dashboard.js returns external dashboard script
        print("[6] Checking GET /static/dashboard.js serving...")
        status, reason, headers, data = send_get("/static/dashboard.js")
        assert status == 200, f"Expected 200 for /static/dashboard.js, got {status}"
        assert "application/javascript" in headers.get("content-type", "").lower()
        js_str = data.decode("utf-8")
        assert "renderTradingViewChart" in js_str, "renderTradingViewChart function missing from dashboard.js"
        print(f"    PASSED: /static/dashboard.js served (HTTP 200, length: {len(data)} bytes).")

        # Test 7: GET /static/nonexistent returns 404
        print("[7] Checking GET /static/nonexistent.js returns 404...")
        status, reason, headers, data = send_get("/static/nonexistent.js")
        assert status == 404, f"Expected 404, got {status}"
        print("    PASSED: Nonexistent static file returned 404.")
    finally:
        server.shutdown()
        server.server_close()

    print("=== DASHBOARD TOKEN TEST PASSED PERFECTLY ===")

if __name__ == "__main__":
    main()
