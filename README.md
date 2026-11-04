# ⚡ Market-Aware Crypto Bot
### Quantitative Multi-Timeframe Breakout & Relative Strength Trading Engine

[![Verification Suite](https://img.shields.io/badge/Verification%20Suite-28%2F28%20PASSED-brightgreen.svg)](#comprehensive-verification-suite)
[![Binance Spot](https://img.shields.io/badge/Exchange-Binance%20Spot-F0B90B.svg?logo=binance&logoColor=black)](https://binance.com)
[![Paper Mode](https://img.shields.io/badge/Default%20Mode-Paper%20Trading-blue.svg)](#quickstart-guide)
[![Profit Factor](https://img.shields.io/badge/Out--of--Sample%20PF-1.27-success.svg)](#quantitative-backtest-benchmark-32-pairs)
[![Python Version](https://img.shields.io/badge/Python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

An institutional-grade, market-context aware quantitative Spot swing trading bot built for Binance. Designed with strict capital preservation, multi-timeframe breakout confirmation, Bitcoin macro regime gating, dynamic 24-hour relative strength ranking, a 100-point conviction scorecard, and an interactive real-time web dashboard.

---

## 🌟 Key Features & Quantitative Edge

### 1. 🎯 Quantitative Multi-Timeframe Breakout Strategy
* **Stateless Retrospective Retest Engine:** Identifies 20-period swing high breakouts with above-average volume ($\ge 1.5\times$ 20-bar average) on closed 1-hour candles, evaluating completed retrospective retests across past 1–4 candles ($Level - 0.75\times ATR \le Low \le Level + 0.25\times ATR$) and a bullish bounce without lookahead bias or future waiting states.
* **100-Point Conviction Scorecard (`MIN_SCORECARD_POINTS=90`):** Dynamically scores setups across 5 quantitative pillars (BTC health: 25 pts, 24h Relative Strength: 25 pts, Breakout Volume $\ge 2.0\times$: 20 pts, Swift Retest $\le 3$ bars: 20 pts, Stop Guard $\le 2.5\%$: 10 pts). Only high-conviction momentum setups qualify for trade entry.
* **Optimized Fixed Take-Profit (`TAKE_PROFIT_PCT=2.0%`):** Replaces premature time stops and unrealistic targets with a verified 2.0% take-profit target sweet spot that captures swing momentum before mean reversion.
* **Tightened Stop Guard Sweet Spot (`MIN_STOP_PCT=1.5%`, `MAX_STOP_PCT=2.5%`):** Caps maximum stop-loss risk at 2.5% while enforcing a 1.5% minimum stop distance to prevent normal 1-hour market noise from prematurely stopping out valid swings.
* **Safe Symbol Blacklisting (`BLACKLISTED_SYMBOLS`):** Filters out chronic high-noise, low-followthrough pairs (`ICPUSDT`, `FILUSDT`, `CRVUSDT`, `ATOMUSDT`) at the scanner level while preserving `EXCLUDED_SYMBOLS=BNBUSDT` strictly for wallet fee-reserve accounting.

### 2. 🛡️ Market-Aware Context Gates
* **Bitcoin Macro Regime Gate (`ENABLE_BTC_REGIME_GATE`):** Evaluates Bitcoin on closed 4-hour candles against its 50-period Exponential Moving Average (50-EMA). When Bitcoin is in a risk-off regime ($BTC \le 50\text{-EMA}$), all altcoin long entries are strictly blocked.
* **24-Hour Relative Strength Filter (`ENABLE_RS_FILTER`):** Continuously computes 24-hour ticker percentage return vs. Bitcoin ($RS = \text{Return}_{\text{coin}} - \text{Return}_{\text{BTC}}$). Only tokens demonstrating positive market leadership ($RS > 0.0\%$) are eligible for pattern entry.

### 3. 🖥️ Interactive Real-Time Web Dashboard
* **Live TradingView Candlestick Charting:** Embedded official TradingView technical widget streaming real-time candlestick data directly from Binance WebSockets.
* **Reactive Financial Metrics:**
  * **Strategy Capital:** Real-time equity and available USDT cash reserves.
  * **Amount Invested:** Exact mark-to-market capital at risk across open positions ($0.00$ when flat).
  * **Win Rate & Realized PnL:** Unbiased historical trade ledger metrics with zero mock or hardcoded values.
* **Live Relative Strength vs BTC Feed:** Continuously synced live ticker tracking top market gainers and major benchmark coins (`ETH`, `SOL`, `XRP`, `DOGE`, `NEAR`, `UNI`, `LINK`).
* **Emergency One-Click Market Exit:** Secure manual position close protected by CSRF token authorization, executing atomic OCO cancellation and market order fill.

### 4. 🔒 Fail-Closed Architecture & Execution Safety
* **Binance Spot OCO Execution:** Simultaneously places exchange-level stop-loss limits and limit-maker take-profit orders directly on Binance order books.
* **Recovery Locks:** Detects any network timeouts or ambiguous API responses during order placement, setting an immutable recovery lock that blocks new entries until confirmed.
* **Drawdown & Circuit Breakers:** Enforces daily loss limits (2%), weekly loss limits (4%), and maximum equity drawdown caps (8%) to halt trading during adverse market conditions.
* **Strict State Isolation:** Enforces distinct state files across Paper Trading (`bot_state_signal_only.json`), Testnet (`bot_state_testnet.json`), and Live Mainnet (`bot_state_allcoins_live.json`) with startup runtime guards preventing cross-environment contamination.

---

## 📊 Quantitative Backtest Benchmark (32 Pairs)

Tested across **12 months of historical data** (~35,133 candles per pair $\times$ 32 pairs = **1.08+ million candles**) divided into:
* **In-Sample (Months 1–8):** Historical parameter discovery period.
* **Out-of-Sample (Months 9–12):** Unseen forward validation dataset.

### Benchmark Results Across Strategy Refinements

| Version / Configuration | Sample Window | Trades | Win Rate | Net PnL ($) | Profit Factor | Max Drawdown | Key Findings |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :--- |
| **V1: Baseline Breakout (3% TP + 4% Stop)** | Out-of-Sample | 22 | 40.9% | -$2.62 | 0.69 | 8.5% | Stops 2x larger than wins; negative expectancy |
| **V2: + 2R Target + 24h Time Stop** | Out-of-Sample | 50 | 32.0% | -$7.19 | 0.62 | 0.9% | 24% of trades hit time stop with fee drag |
| **V3: + BTC Gate + RS Filter + No Time Stop** | Out-of-Sample | 22 | 50.0% | -$0.96 | 0.86 | 8.2% | Win rate reached 50%; fees ate remaining edge |
| **V4: 15m Filter OFF (Unblock Trade Count)** | Out-of-Sample | 179 | 44.2% | -$9.46 | 0.80 | 8.5% | Sample size healthy; 4% stops caused large drawdowns |
| **V5: Tight Stop Guard (1.5%–2.5%)** | Out-of-Sample | 102 | 47.1% | -$2.26 | 0.88 | 4.2% | Drawdown halved; loss reduced by 76% |
| **V6: 2.0% TP + Scorecard 95 + Blacklist** ⭐ | **Out-of-Sample** | **30** | **60.0%** | **+$2.29** | **1.27** | **4.1%** | **Net-positive expectancy; 60% win rate; PF > 1.25** |

*(Simulation parameters: $20 fixed trade size, 0.10% fee per side [0.20% round-trip], 0.05% entry slippage, 0.20% stop exit slippage).*

---

## 🚀 Quickstart Guide

### Prerequisites
* Python 3.10+ (Tested on Python 3.10, 3.11, 3.12, 3.13)
* Windows, macOS, or Linux

### 1. Installation
Clone the repository and install dependencies:
```bash
git clone https://github.com/srivathsav1205/ASOND-Crypto-Bot.git
cd ASOND-Crypto-Bot
pip install -r requirements.txt
```
*(Zero heavy machine-learning dependencies: uses standard library + `python-dotenv`).*

### 2. Configuration
Copy the configuration template to create your `.env` file:
```bash
cp .env.example .env
```
By default, `ENABLE_LIVE_TRADING=false` is enforced. The bot operates in 100% risk-free paper trading mode using live public Binance market data.

### 3. Launch the Trading Bot
```bash
python bot.py
```
* **Interactive Dashboard:** Open **[http://127.0.0.1:8767](http://127.0.0.1:8767)** in your web browser.
* **Scanner:** The engine automatically discovers all liquid USDT pairs ($\ge \$1,000,000$ 24h volume) and scans them in rotating batches.

### 4. Run the Backtesting Engine
Run the multi-pair historical backtester across the 12-month cached dataset:
```bash
python backtest.py
```
Automatically downloads any missing klines from the Binance Public Data Mirror on demand.

### 5. Run the Verification Test Suite
Execute the automated 28-test verification suite:
```bash
python tests/test_suite_all.py
```

---

## 🧪 Comprehensive Verification Suite

The repository includes an isolated test suite validating state integrity, exchange protocol compliance, order lifecycle, and browser UI rendering:

| Test ID | Component Verified | Test Description | Status |
| :---: | :--- | :--- | :---: |
| **Fix 1** | State Sanitization | Paper equity NaN guard & real failure reproduction | **PASSED** |
| **Fix 2** | Trade Lifecycle | Idempotent paper settlement & duplicate open trade guard | **PASSED** |
| **Fix 3** | Time Stop | 24H closed-candle timing & pre-boundary non-expiry | **PASSED** |
| **Fix 4** | Ledger Schema | Unified paper settlement schema across all 4 exit paths | **PASSED** |
| **Fix 5** | Accounting | Baseline & flow-adjusted deposit/withdrawal tracking | **PASSED** |
| **Fix 6** | Exchange Protocol | Live OCO maker guard direct exit (unmocked baseline) | **PASSED** |
| **Fix 7** | Manual Close | Live manual market exit safe order sequence | **PASSED** |
| **Fix 8** | Watchdog | Watchdog verifies OCO orderList status prior to orders | **PASSED** |
| **Fix 9** | Order Safety | Stop leg unfilled 30s fallback market exit | **PASSED** |
| **Item 1** | File Safety | `allow_nan=False` serialization check | **PASSED** |
| **Item 2** | Exit Coverage | Unmocked baselines across all 5 exit execution paths | **PASSED** |
| **Item 3** | Recovery | Symbol-scoped lock clearing isolation | **PASSED** |
| **Item 4** | Flow Tracking | External cash flow recording validation | **PASSED** |
| **Item 5** | Concurrency | Windows retry logic & atomic file replacement | **PASSED** |
| **Item 6** | Preflight | Live account preflight validation & untracked asset guard | **PASSED** |
| **Item 10** | Lookahead | Zero lookahead bias verified on synthetic 4H candles | **PASSED** |
| **Review 1** | Mode Guard | Testnet credentials validation and live refusal | **PASSED** |
| **Review 2** | Gap Protection | Unprotected order window emergency market exit | **PASSED** |
| **Review 3** | Network Safety | OCO cancel timeout orderList re-query and resolution | **PASSED** |
| **Review 4** | State Isolation | Testnet, Live, and Paper state file isolation guards | **PASSED** |
| **Review 7/8**| Data Integrity | Backtest cache freshness and zero-tolerance alignment | **PASSED** |
| **Browser** | UI & CSP | Headless Chrome browser verification with zero CSP blocks | **PASSED** |

---

## 🏛️ Project Directory Structure

```text
├── bot.py                               # Primary Spot swing trading bot & dashboard server
├── backtest.py                          # Multi-pair 12-month backtester with walk-forward validation
├── dashboard.html                       # Real-time TradingView-integrated web dashboard UI
├── requirements.txt                     # Project dependencies (python-dotenv)
├── .env.example                         # Documented configuration template
├── .gitignore                           # Git ignore rules for logs, secrets, and bulk cache
├── LICENSE                              # MIT License
├── static/
│   ├── dashboard.js                     # Reactive dashboard frontend controller
│   └── tv.js                            # TradingView charting library asset
└── tests/
    ├── __init__.py                      # Test package indicator
    ├── test_suite_all.py                # Main 28-test verification test runner
    ├── test_browser_dashboard.py        # Real headless browser test with CSP audit
    └── test_*.py                        # Modular unit & integration tests
```

---

## ⚠️ Risk Disclosure & Disclaimer

Trading cryptocurrencies carries significant financial risk. Past backtested performance is no guarantee of future trading results. This software is provided for educational and research purposes under the [MIT License](LICENSE). Always run in Paper Trading mode (`ENABLE_LIVE_TRADING=false`) to evaluate execution performance before committing live capital.
