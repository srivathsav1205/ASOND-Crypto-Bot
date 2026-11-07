# Operational Runbook: Asond Crypto Bot
### Procedures for Deployment, Monitoring, Risk Controls, and Emergency Operations

---

## Table of Contents
1. [System Architecture and Runtime Specifications](#1-system-architecture-and-runtime-specifications)
2. [Environment Configuration and Execution Modes](#2-environment-configuration-and-execution-modes)
3. [Standard Operating Procedures](#3-standard-operating-procedures)
   * [3.1 Running Paper Simulation (Default)](#31-running-paper-simulation-default)
   * [3.2 State Persistence and Resumption](#32-state-persistence-and-resumption)
   * [3.3 Promoting to Live Mainnet Execution](#33-promoting-to-live-mainnet-execution)
4. [Monitoring and Spot Execution Terminal](#4-monitoring-and-spot-execution-terminal)
5. [Telegram Remote Command Interface](#5-telegram-remote-command-interface)
6. [Emergency Procedures and Incident Response](#6-emergency-procedures-and-incident-response)
7. [Backtest and Verification Testing](#7-backtest-and-verification-testing)

---

## 1. System Architecture and Runtime Specifications

* **Runtime:** Python 3.10, 3.11, 3.12, 3.13
* **Primary Daemon:** `bot.py`
* **Operating Systems:** Windows 10/11, macOS, Linux (Ubuntu 20.04+)
* **Dependencies:** `aiohttp>=3.9.0`, `python-dotenv>=1.0.0`, `matplotlib>=3.8.0`
* **Network Connectivity:**
  * Binance REST API: `https://api.binance.com`
  * Binance WebSocket Streams: `wss://stream.binance.com:9443/stream?streams=`
  * Historical Archives: `https://data.binance.vision`
* **Local Execution Terminal:** HTTP port `8767` on `127.0.0.1`
* **Universe Capacity:** 395+ active Binance USDT spot trading pairs streamed in parallel across 4 socket clusters.

---

## 2. Environment Configuration and Execution Modes

All runtime options are configured via `.env` in the root workspace.

### Core Parameter Reference (.env)

```ini
# Execution Mode
DRY_RUN=true

# Capital Allocation and Exposure
TRADE_USDT=5.0
MAX_OPEN_TRADES=20
SYMBOL_COOLDOWN_SEC=172800

# Volatility Signal Thresholds
PUMP_1M_PCT=5.0
PUMP_1S_PCT=1.0
MIN_QUOTE_VOL_1M=0.0
MIN_QUOTE_VOL_1S=0.0

# Regime and Health Filters
SKIP_24H_LOSERS=true
MIN_24H_CHANGE_PCT=0.0
SKIP_HELD_COINS=true

# Exit Architecture (Ladder Mode)
EXIT_MODE=ladder
LADDER_TARGETS_PCT=3,5,10,15,25,50
LADDER_STOP_MODE=previous
LADDER_FIRST_STOP_PCT=0.2
LADDER_SELL_AT_LAST=true
LADDER_EXCHANGE_STOP=true

# Risk Management
STOP_LOSS_ENABLED=false
STOP_LOSS_PCT=3.0
HOLD_DRAWDOWN_PCT=20.0

# Terminal Server
DASHBOARD_ENABLED=true
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8767
DASHBOARD_TOKEN=

# Optional Telegram Notifications
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_SNAPSHOTS=true
TELEGRAM_SUMMARY_MINUTES=60
```

---

## 3. Standard Operating Procedures

### 3.1 Running Paper Simulation (Default)

1. Verify `.env` contains:
   ```ini
   DRY_RUN=true
   EXIT_MODE=ladder
   ```
2. Start the daemon process:
   ```bash
   python bot.py
   ```
3. Verify startup in the console output:
   * Confirms `DRY RUN` mode active.
   * Confirms 4 WebSocket clusters connected (e.g. 100 symbols per socket).
   * Confirms dashboard listening on `http://127.0.0.1:8767`.
4. Open the Spot Execution Terminal at **`http://127.0.0.1:8767`**.

### 3.2 State Persistence and Resumption

The bot automatically persists its state on every position open, target update, stop ratchet, or exit:
* In Paper Mode (`DRY_RUN=true`), state is saved to `positions_dry.json`.
* In Live Mode (`DRY_RUN=false`), state is saved to `positions.json`.
* **Restart Survival:** If the daemon process is terminated or the machine restarts, relaunching `python bot.py` immediately loads existing positions and resumes active tracking and order reconciliation.

### 3.3 Promoting to Live Mainnet Execution

Before enabling real capital execution, complete the following pre-flight verification:

#### Pre-Flight Checklist
- [ ] At least 48 hours of continuous, error-free paper execution.
- [ ] `BINANCE_API_KEY` and `BINANCE_API_SECRET` are generated on Binance with **Spot Trading enabled** and **Withdrawals strictly disabled**.
- [ ] IP address restriction is enabled on the Binance API management console.
- [ ] Account holds dedicated USDT cash reserve.
- [ ] Account holds a small BNB balance to cover spot trading fees with a 25% discount.
- [ ] Verify `TRADE_USDT` is set above Binance minimum order notional ($5.00 to $10.00 depending on pair rules; recommend $10.00 to prevent `MIN_NOTIONAL` rejection).

#### Activation Sequence
1. In `.env`, set:
   ```ini
   DRY_RUN=false
   BINANCE_API_KEY=your_production_key
   BINANCE_API_SECRET=your_production_secret
   ```
2. Launch the daemon:
   ```bash
   python bot.py
   ```
3. Inspect `bot.log` to confirm account balances are fetched and the startup notice indicates `[LIVE]`.

---

## 4. Monitoring and Spot Execution Terminal

The local web terminal (`http://127.0.0.1:8767`) provides complete real-time observability:

* **Exposure Bar:**
  * `Exposure / Concurrency`: Active positions count versus max capacity (e.g. `2 / 20`).
  * `Realized Net PnL`: Total cumulative realized gains/losses from completed sells.
  * `Active Positions PnL`: Mark-to-market unrealized gain or loss on open trades.
  * `Parked Holdings PnL`: Mark-to-market unrealized loss on positions held under the -20% rule.
  * `Operational Uptime`: Continuous runtime counter.
* **Active Open Positions Table:**
  * Shows entry price, current mark price, net PnL with return percentage.
  * Interactive 6-step ladder visualization (`+3%`, `+5%`, `+10%`, `+15%`, `+25%`, `+50%`) indicating completed milestones.
  * Active trailing stop status (e.g. `Stop: $0.0082 (T1, after T2)`).
  * One-click `Close` button to manually market-sell any single position.
* **Parked Holdings Table:**
  * Displays positions down >= 20% that were parked to avoid locking active slots.
  * Shows entry cost, current price, and cumulative drawdown.
* **48-Hour Cooldown Filter:**
  * Lists recently bought tokens and hours/minutes remaining before new entries are permitted.
* **Real-Time Volatility Signals Stream:**
  * Chronological log of all detected 1m and 1s candle spikes with execution verdicts (`BUY` or skipped reasons).
* **Activity Event Log:**
  * System event stream with timestamps detailing trade entries, ladder ratchets, stop updates, and error alerts.

---

## 5. Telegram Remote Command Interface

When `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are configured, the bot provides interactive remote control:

* `/status` : Full engine operational status, uptime, pair count, active exit configuration, and realized PnL.
* `/trades` : List of all currently open positions with current mark prices and ladder stages.
* `/holdings` : Summary of tokens currently parked in long-term holdings.
* `/cooldown` : Coins currently restricted under the 48-hour cooldown rule.
* `/pause` : Suspends new buy signals while continuing to manage active open positions.
* `/resume` : Clears pause or emergency halt states, allowing new buy executions.
* `/closeall` : Prompts confirmation to market-sell every active open position immediately.

---

## 6. Emergency Procedures and Incident Response

### Scenario A: Suspending New Entries Immediately
If market conditions become hostile (e.g. broad market crash):
1. **Via Web Terminal:** Click the yellow **Pause** button in the header.
2. **Via Telegram:** Send `/pause`.
3. Active trades remain monitored and exit according to their ladder stops, but all new spike signals are skipped.

### Scenario B: Emergency Market Liquidation of All Active Trades
If you need to exit all open exposure immediately:
1. **Via Web Terminal:** Click the white **Sell All Positions** button and confirm the modal prompt.
2. **Via Telegram:** Send `/closeall`.
3. The engine sends cancel requests for active stops and executes market sells for all open positions. Note: Long-term parked holdings are untouched by default.

### Scenario C: Complete Engine Halt (Kill Switch)
To halt all operations and block any future buys across restarts:
1. **Via Web Terminal:** Click the red **Halt Engine** button.
2. **Via Telegram:** Send `/kill`.
3. The engine enters the `killed` state, persisted in state storage. No new buys will execute until `/resume` is sent or cleared in the state file.

### Scenario D: Graceful Process Shutdown
To stop the bot daemon on the server:
1. In the active shell window, press `Ctrl+C`.
2. The engine executes its cleanup routine, saves current positions to `positions_dry.json` (or `positions.json`), and disconnects WebSocket streams.
3. In Live mode, positions remain protected on Binance by their native STOP_LOSS orders.

---

## 7. Backtest and Verification Testing

### Running the 1-Minute Walk-Forward Backtester
To evaluate the strategy across 1-minute historical data over the two standardized 90-day market regimes:
```bash
python tests/test_pump_scalper_ea6ff1b3.py
```
* Simulates the exact $5.00 trade sizing, 0.3% entry slippage, 0.1% fees, 48h cooldown, and max 20 positions.
* Exports raw trade logs and dual-accounting tables to `tests/pump_scalper_results.json`.

### Auditing the Spot Execution Terminal
To verify that the web dashboard conforms to zero-emoji, no purple gradients, and zero em-dash constraints:
```bash
python tests/audit_dashboard.py
```
Ensure all 10 automated compliance checks report `PASS`.
