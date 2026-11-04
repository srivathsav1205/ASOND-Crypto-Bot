import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

import bot
import threading
import time
import subprocess
import tempfile
import shutil
from http.server import ThreadingHTTPServer

server = ThreadingHTTPServer(('127.0.0.1', 0), bot.DashboardHandler)
port = server.server_address[1]
t = threading.Thread(target=server.serve_forever, daemon=True)
t.start()

user_data = tempfile.mkdtemp()
screenshot_path = Path("tests/dashboard_screenshot.png").resolve()
cmd = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "--headless=new",
    "--disable-gpu",
    f"--user-data-dir={user_data}",
    f"--screenshot={screenshot_path}",
    "--window-size=1280,800",
    "--dump-dom",
    f"http://127.0.0.1:{port}/"
]
res = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=15)
dom = res.stdout
shutil.rmtree(user_data, ignore_errors=True)
server.shutdown()

print("DOM len:", len(dom))
needle = 'id="tv_chart_container"'
pos = dom.find(needle)
print("Position of needle:", pos)
if pos != -1:
    snippet = dom[pos:pos+1000]
    print("Snippet:")
    print(snippet)
    has_iframe = "<iframe" in snippet.lower()
    print("Contains iframe directly inside container snippet:", has_iframe)

print("Screenshot exists:", screenshot_path.exists())
if screenshot_path.exists():
    print("Screenshot size:", screenshot_path.stat().st_size)
