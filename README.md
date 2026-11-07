# Asond Crypto Bot
### Institutional Binance Spot Volatility Capture Engine and Execution Terminal

[![Python Version](https://img.shields.io/badge/Python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://python.org)
[![Exchange](https://img.shields.io/badge/Exchange-Binance%20Spot-F0B90B.svg?logo=binance&logoColor=black)](https://binance.com)
[![Mode](https://img.shields.io/badge/Execution%20Mode-Paper%20Trading%20(DRY%20RUN)-green.svg)](#quickstart-guide)
[![License](https://img.shields.io/badge/License-MIT-gray.svg)](LICENSE)

Asond Crypto Bot is an institutional-grade cryptocurrency execution engine built for Binance Spot. Designed for high-frequency volatility spike capture across all active USDT pairs, multi-stream WebSocket market monitoring, six-tier ratcheting ladder stop exits, automated capital recycling, and an integrated real-time web execution terminal.

---

## Strategy Architecture and Execution Logic

### 1. Volatility Spike Detection
* **Multi-Stream WebSocket Scanning:** Establishes parallel WebSocket streams monitoring 395+ active Binance USDT spot pairs simultaneously at 1-minute and 1-second kline resolution.
* **Entry Trigger:** Automatically executes when an individual kline surges above its opening price:
  * 1-minute candle: `high >= open * 1.05` (+5.0% surge)
  * 1-second candle: `high >= open * 1.01` (+1.0% surge)
* **Execution Pricing:** Modeled with realistic fill pricing incorporating 0.30% entry slippage:
  $$\text{Entry Price} = (\text{Open} \times 1.05) \times (1 + 0.003)$$
* **Trading Fees:** Enforces 0.10% maker/taker fee per side (0.20% round-trip).

### 2. Multi-Stage Risk and Regime Filters
* **24-Hour Loser Filter (`SKIP_24H_LOSERS = True`):** Blocks entries on any token whose rolling 24-hour price change is negative (`change_24h < 0.0%`), preventing knife-catching on macro downtrends.
* **48-Hour Per-Coin Cooldown (`SYMBOL_COOLDOWN_SEC = 172800`):** Once a token triggers an entry, new buys on that token are restricted for 48 hours to avoid repeated exposure during volatile chop.
* **Skip Held Tokens (`SKIP_HELD_COINS = True`):** Prevents re-buying any token that currently sits in long-term holdings.
* **Exposure and Concurrency Caps:** Strictly caps maximum open positions at 20 (`MAX_OPEN_TRADES = 20`) with a fixed $5.00 USDT allocation per trade (`TRADE_USDT = 5.0`), establishing a hard $100.00 maximum active capital cap.

### 3. Exit Modes

#### Mode A: Six-Tier Ratcheting Ladder (`EXIT_MODE = ladder`, Default)
The ladder exit lets winning trades ride explosive surges while locking in profits step-by-step:
* **Target 1 (+3.0%):** Does not sell. Stop loss moves up to Breakeven +0.2% (covers round-trip fees).
* **Target 2 (+5.0%):** Does not sell. Stop loss moves up to Target 1 (+3.0%).
* **Target 3 (+10.0%):** Does not sell. Stop loss moves up to Target 2 (+5.0%).
* **Target 4 (+15.0%):** Does not sell. Stop loss moves up to Target 3 (+10.0%).
* **Target 5 (+25.0%):** Does not sell. Stop loss moves up to Target 4 (+15.0%).
* **Target 6 (+50.0%):** Sells 100% of the position at market (`LADDER_SELL_AT_LAST = True`).
* **Initial Stop:** No stop-loss prior to reaching Target 1 (`STOP_LOSS_ENABLED = False`). Trades are managed by the -20% hold rule.
* **Exchange-Level Protection (`LADDER_EXCHANGE_STOP = True`):** Submits real STOP_LOSS_LIMIT orders to Binance to protect positions against software or network interruptions.

#### Mode B: Fixed Take-Profit (`EXIT_MODE = tp`)
Places an immediate limit sell order at +2.0% above the average fill price with optional -3.0% hard stop loss.

### 4. The -20% Long-Term Hold Rule
* If an open position experiences a -20.0% drawdown from its entry price before reaching Target 1, all active sell orders are cancelled and the token is moved to `holdings`.
* The position is never sold at a market loss by the engine.
* Crucially, **the active trading slot is immediately freed**, allowing the engine to deploy capital into new opportunities.

---

## Quantitative Backtest Benchmarks

Evaluated across **two separate 90-day market regimes** using true 1-minute Binance Spot historical klines (100 high-activity pairs, ~864,000 candles per evaluation window) with full fee (0.10%/side) and slippage (0.30% buy, 0.10% stop) modeling.

### 1. Comparative Performance Tables ($5.00/Trade Basis)

#### Window 2: January 1 to March 31, 2026 (Choppy / Consolidation Regime)

| Exit Mode | Accounting View | Trades | Win Rate | Total Net PnL | Parked Count | Average Parked Return |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| **TP Mode (+2% Target)** | **Bot Own Accounting** (Parked Excluded) | 89 closed | **100.0%** | **+$8.00** | 10 bags ($50 frozen) | *Excluded from ledger* |
| | **TRUE: Window End Mark-to-Market** | 99 total | **89.9%** | **-$5.91** *(Net Loss)* | 10 bags ($50 frozen) | **-27.8%** |
| | **TRUE: +7 Days Post-Parking** | 99 total | **89.9%** | **-$0.88** *(Net Loss)* | 10 bags ($50 frozen) | **-17.7%** |
| | **TRUE: +30 Days Post-Parking** | 99 total | **90.9%** | **-$2.80** *(Net Loss)* | 10 bags ($50 frozen) | **-21.6%** |
| **Ladder Mode (3/5/10/15/25/50%)** | **Bot Own Accounting** (Parked Excluded) | 84 closed | **73.8%** | **+$16.63** | 13 bags ($65 frozen) | *Excluded from ledger* |
| | **TRUE: Window End Mark-to-Market** | 98 total | **63.3%** | **-$1.36** *(Net Loss)* | 13 bags ($65 frozen) | **-26.5%** |
| | **TRUE: +7 Days Post-Parking** | 98 total | **63.3%** | **+$3.98** | 13 bags ($65 frozen) | **-18.3%** |
| | **TRUE: +30 Days Post-Parking** | 98 total | **64.3%** | **+$2.56** | 13 bags ($65 frozen) | **-20.5%** |

#### Window 1: July 1 to September 30, 2026 (Bull / Momentum Regime)

| Exit Mode | Accounting View | Trades | Win Rate | Total Net PnL | Parked Count | Average Parked Return |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| **TP Mode (+2% Target)** | **Bot Own Accounting** (Parked Excluded) | 123 closed | **100.0%** | **+$11.06** | 12 bags ($60 frozen) | *Excluded from ledger* |
| | **TRUE: Window End Mark-to-Market** | 135 total | **94.8%** | **+$12.34** | 12 bags ($60 frozen) | **+2.1%** |
| | **TRUE: +7 Days Post-Parking** | 135 total | **91.9%** | **-$0.41** *(Net Loss)* | 12 bags ($60 frozen) | **-19.1%** |
| | **TRUE: +30 Days Post-Parking** | 135 total | **92.6%** | **+$6.31** | 12 bags ($60 frozen) | **-7.9%** |
| **Ladder Mode (3/5/10/15/25/50%)** | **Bot Own Accounting** (Parked Excluded) | 112 closed | **60.7%** | **+$15.63** | 17 bags ($85 frozen) | *Excluded from ledger* |
| | **TRUE: Window End Mark-to-Market** | 129 total | **57.4%** | **+$20.86** | 17 bags ($85 frozen) | **+6.2%** |
| | **TRUE: +7 Days Post-Parking** | 129 total | **54.3%** | **+$12.57** | 17 bags ($85 frozen) | **-3.6%** |
| | **TRUE: +30 Days Post-Parking** | 129 total | **55.0%** | **+$10.63** | 17 bags ($85 frozen) | **-5.9%** |

### 2. Empirical Findings: Survivorship Bias in the Hold Rule
* **The 100% Win Rate Mirage:** In TP mode, the bot's internal accounting reports a 100% win rate because positions that plunge past -20% are never sold at market. They are moved into `holdings` and removed from the closed trade history.
* **Asymmetric Risk Exposure:** In choppy markets (Window 2), 10 positions hit the -20% hold rule. Tokens like `ZENUSDT` (-58.9%) and `BELUSDT` (-43.2%) crashed significantly further after being parked, erasing all accumulated scalping profits when marked to market.
* **Ladder Outperformance:** The six-tier ratcheting ladder significantly outperforms fixed TP by capturing +10%, +15%, and +25% runners during strong trends while maintaining positive net expectancy under true accounting.

---

## Real-Time Spot Execution Terminal

The engine includes a self-contained web dashboard running locally at **[http://127.0.0.1:8767](http://127.0.0.1:8767)**:
* **Zero Dependencies:** Built entirely with native vanilla JavaScript and CSS (no external CDN libraries, no runtime script bloat).
* **Live Telemetry:** Auto-polls `/api/state` every 1.5 seconds to display current exposure, cash reserves, realized PnL, and unrealized return.
* **Ladder Progress Visualizer:** Shows active positions with interactive step indicators (`+3%`, `+5%`, `+10%`, `+15%`, `+25%`, `+50%`), next target price, and active trailing stops.
* **Operational Controls:** Interactive execution controls to **Pause Engine**, **Sell Position**, **Sell All Positions**, or **Emergency Halt**.
* **Integrated Legal Views:** Built-in Privacy Policy (`#privacy`) and Terms and Conditions (`#terms`) routes directly accessible from the navigation bar.

---

## Quickstart Guide

### 1. Requirements
* Python 3.10, 3.11, 3.12, or 3.13
* Windows, macOS, or Linux

### 2. Installation
```bash
git clone https://github.com/srivathsav1205/ASOND-Crypto-Bot.git
cd ASOND-Crypto-Bot
pip install -r requirements.txt
```

### 3. Configuration
The `.env` file controls all execution rules and risk parameters:
```env
# Execution Mode (DRY_RUN=true for paper simulation)
DRY_RUN=true
TRADE_USDT=5
MAX_OPEN_TRADES=20
SYMBOL_COOLDOWN_SEC=172800

# Exit Architecture
EXIT_MODE=ladder
LADDER_TARGETS_PCT=3,5,10,15,25,50
LADDER_STOP_MODE=previous
LADDER_FIRST_STOP_PCT=0.2
STOP_LOSS_ENABLED=false
HOLD_DRAWDOWN_PCT=20

# Dashboard Server
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8767
```

### 4. Running the Bot
Launch the primary trading daemon:
```bash
python bot.py
```
Open **[http://127.0.0.1:8767](http://127.0.0.1:8767)** in any modern web browser to monitor live execution.

### 5. Running the Backtest Harness
To reproduce the 1-minute historical backtest across the two 90-day windows:
```bash
python tests/test_pump_scalper_ea6ff1b3.py
```
Raw outputs and trade-by-trade logs are exported directly to `tests/pump_scalper_results.json`.

---

## Project Structure

```text
├── bot.py                               # Primary running pump-scalper daemon and terminal server
├── dashboard.html                       # Real-time web execution terminal interface
├── requirements.txt                     # Python dependencies (aiohttp, python-dotenv, matplotlib)
├── .env                                 # Active runtime configuration file
├── .env.example                         # Reference configuration template
├── positions_dry.json                   # Real-time state persistence for paper positions
├── bot.log                              # Active engine execution log
├── legacy_breakout_bot/                 # Preserved archive of original multi-timeframe swing bot
│   └── bot.py                           # Original breakout/retest bot code
├── backtest.py                          # 12-month swing breakout backtesting engine
├── data/                                # Primary historical candle repository
│   ├── *_15m_12m.json                   # 12-month 15m klines for 32 major pairs
│   ├── *_1h_12m.json                    # 12-month 1h klines for 32 major pairs
│   ├── *_4h_12m.json                    # 12-month 4h klines for 32 major pairs
│   └── cache_1m/                        # 1-minute monthly kline archives from Binance Vision
├── data_v2/                             # CSV formatted historical candles
└── tests/
    ├── test_pump_scalper_ea6ff1b3.py    # Walk-forward 1-minute pump scalper backtest harness
    ├── pump_scalper_results.json        # Raw backtest results and accounting logs
    └── valid_symbols.json               # Universe definition of 369 Binance Spot USDT pairs
```

---

## Risk Disclosure

Trading digital assets involves substantial risk of financial loss. Sudden price surges often experience aggressive mean-reversion pullbacks. Holding drawdowns past -20% carries risk of prolonged capital lockup or total capital impairment. Always evaluate performance in Paper Trading mode (`DRY_RUN = True`) before connecting live capital. Provided for quantitative research purposes under the [MIT License](LICENSE).
