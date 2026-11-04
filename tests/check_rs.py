import urllib.request
import json

req = urllib.request.Request("https://api.binance.com/api/v3/ticker/24hr", headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=15) as resp:
    data = json.loads(resp.read().decode())

btc = next(t for t in data if t["symbol"] == "BTCUSDT")
btc_chg = float(btc["priceChangePercent"])
print(f"Binance Live BTCUSDT: price={btc['lastPrice']}, 24h_change={btc_chg:+.2f}%")

rs_list = []
for t in data:
    sym = t["symbol"]
    if not sym.endswith("USDT"):
        continue
    vol = float(t.get("quoteVolume", 0))
    if vol < 1000000:
        continue
    chg = float(t.get("priceChangePercent", 0))
    rs = chg - btc_chg
    rs_list.append((sym, rs, chg, float(t["lastPrice"]), vol))

rs_list.sort(key=lambda x: -x[1])
print("\nTop 10 Live RS Leaders directly from Binance:")
for s, rs, chg, px, vol in rs_list[:10]:
    print(f"  {s:<14} | Price: {px:<10.6g} | Coin 24h: {chg:+.2f}% | BTC 24h: {btc_chg:+.2f}% | RS: {rs:+.2f}% | Vol: ${vol/1e6:.1f}M")

print("\nMajor Coins RS vs BTC:")
major = ["ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "NEARUSDT", "UNIUSDT", "LINKUSDT"]
for s, rs, chg, px, vol in rs_list:
    if s in major:
        print(f"  {s:<14} | Price: {px:<10.6g} | Coin 24h: {chg:+.2f}% | BTC 24h: {btc_chg:+.2f}% | RS: {rs:+.2f}% | Vol: ${vol/1e6:.1f}M")
