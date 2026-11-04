import urllib.request
import json
import time

t0 = time.time()
symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BEAMXUSDT", "NEARUSDT", "XRPUSDT", "DOGEUSDT", "UNIUSDT"]
encoded = json.dumps(symbols, separators=(",", ":"))
url = f"https://api.binance.com/api/v3/ticker/24hr?symbols={urllib.request.quote(encoded)}"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=5) as r:
    data = json.loads(r.read().decode())
elapsed = time.time() - t0
print(f"Fetched {len(data)} symbols in {elapsed:.3f}s:")
btc_chg = next((float(d["priceChangePercent"]) for d in data if d["symbol"] == "BTCUSDT"), 0.0)
for d in data:
    sym = d["symbol"]
    px = float(d["lastPrice"])
    chg = float(d["priceChangePercent"])
    rs = chg - btc_chg
    print(f"  {sym:<10} | Price: {px:<10.4f} | 24h: {chg:+.2f}% | RS vs BTC: {rs:+.2f}%")
