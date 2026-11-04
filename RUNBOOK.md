# 📖 Operational Runbook: Binance Spot Swing Trading Engine

This runbook covers step-by-step procedures for deployment, operation, monitoring, maintenance, and emergency response for the **Binance Spot Swing Bot**.

---

## 📑 Table of Contents
1. [System Requirements & Architecture](#1-system-requirements--architecture)
2. [Environment Configuration & Modes](#2-environment-configuration--modes)
3. [Standard Operating Procedures](#3-standard-operating-procedures)
   * [3.1 Starting Paper Trading (Default)](#31-starting-paper-trading-default)
   * [3.2 Starting Binance Testnet Mode](#32-starting-binance-testnet-mode)
   * [3.3 Promoting to Production Live Trading](#33-promoting-to-production-live-trading)
4. [Monitoring & Web Dashboard](#4-monitoring--web-dashboard)
5. [Capital Management & External Flows](#5-capital-management--external-flows)
6. [Emergency Procedures & Incident Response](#6-emergency-procedures--incident-response)
7. [Verification & Maintenance](#7-verification--maintenance)

---

## 1. System Requirements & Architecture

* **Runtime:** Python 3.10+ (Tested on 3.10, 3.11, 3.12, 3.13)
* **OS:** Windows 10/11, macOS, Linux (Ubuntu 20.04+)
* **Dependencies:** Standard library only + `python-dotenv>=1.0.0`
* **Network:** Outbound HTTPS access to Binance API endpoints:
  * Public Data: `https://data-api.binance.vision`
  * Mainnet Live: `https://api.binance.com`
  * Testnet: `https://testnet.binance.vision`
* **Local Web Dashboard:** HTTP port `8767` on `127.0.0.1`

---

## 2. Environment Configuration & Modes

The bot operates in three mutually isolated execution environments:

| Feature / Setting | Paper Trading (Default) | Binance Testnet | Production Live Trading |
| :--- | :--- | :--- | :--- |
| `ENABLE_LIVE_TRADING` | `false` | `true` | `true` |
| `CONFIRM_LIVE_TRADING` | *(empty)* | `YES_I_ACCEPT_RISK` | `YES_I_ACCEPT_RISK` |
| `BINANCE_BASE_URL` | `https://data-api.binance.vision` | `https://testnet.binance.vision` | `https://api.binance.com` |
| State File Enforced | `bot_state_signal_only.json` | `bot_state_testnet.json` | `bot_state_allcoins_live.json` |
| Capital at Risk | **$0.00 (Simulated)** | **$0.00 (Testnet Funds)** | **Real Strategy Capital** |

### Verified Parameter Set (.env)
```ini
# Strategy Parameters (Walk-Forward Verified)
EXIT_MODE=FIXED_TP
TAKE_PROFIT_PCT=2.0
MIN_STOP_PCT=1.5
MAX_STOP_PCT=2.5
MAX_HOLD_HOURS=0
ENABLE_15M_FILTER=false
MIN_SCORECARD_POINTS=95

# Context Gates & Filters
ENABLE_BTC_REGIME_GATE=true
ENABLE_RS_FILTER=true

# Symbol Management
SCAN_ALL_USDT_SPOT=true
MIN_24H_QUOTE_VOLUME_USDT=1000000
EXCLUDED_SYMBOLS=BNBUSDT
BLACKLISTED_SYMBOLS=ICPUSDT,FILUSDT,CRVUSDT,ATOMUSDT
```

---

## 3. Standard Operating Procedures

### 3.1 Starting Paper Trading (Default)
1. Verify `.env` has `ENABLE_LIVE_TRADING=false`.
2. Start the bot daemon:
   ```bash
   python bot.py
   ```
3. Open your browser to `http://127.0.0.1:8767`.
4. Observe the console logs:
   ```text
   INFO SIGNAL-ONLY / PAPER MODE ACTIVE. No real money or exchange orders will be placed.
   INFO Exchange symbol metadata loaded: eligible pairs=503
   INFO USDT Spot scan universe refreshed: 503 eligible pairs, 131 pass 24h liquidity floor.
   INFO Dashboard active at: http://127.0.0.1:8767
   ```

### 3.2 Starting Binance Testnet Mode
1. Acquire testnet API keys from [https://testnet.binance.vision](https://testnet.binance.vision).
2. Configure `.env`:
   ```ini
   BINANCE_BASE_URL=https://testnet.binance.vision
   BINANCE_TESTNET_API_KEY=your_testnet_key
   BINANCE_TESTNET_API_SECRET=your_testnet_secret
   ENABLE_LIVE_TRADING=true
   CONFIRM_LIVE_TRADING=YES_I_ACCEPT_RISK
   BOT_STATE_FILE=bot_state_testnet.json
   ```
3. Run the bot:
   ```bash
   python bot.py
   ```
4. Verify in logs:
   ```text
   INFO Binance Spot Testnet mode confirmed. Orders will be sent to testnet.binance.vision.
   ```

### 3.3 Promoting to Production Live Trading
> **WARNING:** Never switch to live trading without completing the pre-flight checklist.

#### Pre-Flight Checklist
- [ ] At least 48 hours of error-free paper trading runs.
- [ ] Account holds dedicated USDT trading capital (zero untracked altcoins with value > $1.00).
- [ ] Account holds BNB balance for spot fee discounts (`EXCLUDED_SYMBOLS=BNBUSDT`).
- [ ] Zero active open orders on Binance Spot.
- [ ] IP address whitelisted on Binance API key management console.
- [ ] API key has **Spot Trading enabled** and **Withdrawals disabled**.

#### Activation Sequence
1. Set `.env`:
   ```ini
   BINANCE_BASE_URL=https://api.binance.com
   BINANCE_API_KEY=your_real_key
   BINANCE_API_SECRET=your_real_secret
   ENABLE_LIVE_TRADING=true
   CONFIRM_LIVE_TRADING=YES_I_ACCEPT_RISK
   BOT_STATE_FILE=bot_state_allcoins_live.json
   ```
2. Run live preflight initialization:
   ```bash
   python initialize_allcoin_live_state.py
   ```
   Ensure the output confirms: `LIVE STATE INITIALIZED SUCCESSFULLY`.
3. Launch the live bot:
   ```bash
   python bot.py
   ```

---

## 4. Monitoring & Web Dashboard

The local web dashboard (`http://127.0.0.1:8767`) provides real-time system observability:

* **TradingView Chart Widget:** Switch between pairs to inspect live price action against the 1H breakout levels.
* **Capital Cards:**
  * **Strategy Capital:** Reflects total USDT equity (free cash + position mark-to-market).
  * **Amount Invested:** Capital actively deployed in open positions ($0.00 when flat).
  * **Win Rate & Realized PnL:** Cumulative performance computed directly from closed trades.
* **Relative Strength Feed:** Real-time ticker comparing 24-hour token performance against BTC.
* **Position Control Table:** Lists open positions with entry, stop-loss, take-profit, and a one-click manual close button.

---

## 5. Capital Management & External Flows

When depositing or withdrawing funds during live trading, update the flow ledger to prevent false drawdown alerts or equity mismatch locks.

### Recording an External Deposit
```bash
python record_external_flow.py --deposit 500.0 --note "Added operating capital"
```

### Recording an External Withdrawal
```bash
python record_external_flow.py --withdraw 200.0 --note "Profit withdrawal"
```

---

## 6. Emergency Procedures & Incident Response

### Scenario A: Emergency Manual Position Exit
If you need to close a position immediately:
1. **Via Dashboard:** Click the red **"Close at Market"** button next to the position.
2. **Via Code/CLI:** Run:
   ```python
   python -c "import bot; bot.SwingBot().close_position_at_market('ETHUSDT')"
   ```
3. The engine automatically cancels the active OCO orders first, then executes a market sell.

### Scenario B: Recovery Lock Engaged
If an API timeout occurs during order placement, the bot sets `recovery_required` to fail closed:
1. Check Binance mobile app or web interface to see if the order filled.
2. If position is open without an OCO, place the stop-loss manually on Binance.
3. Once verified, clear the recovery lock by editing `bot_state_*.json`:
   ```json
   "recovery_required": null,
   "manual_pause": false
   ```
4. Restart the bot.

### Scenario C: Stopping the Daemon Safely
* **Windows / Linux:** Press `Ctrl+C` or kill the process gracefully.
* Existing open positions in Live mode remain fully protected on Binance by their active OCO exchange orders.
* In Paper mode, open positions persist in `bot_state_signal_only.json` and resume monitoring upon restart.

---

## 7. Verification & Maintenance

Run the automated verification test suite periodically or after updating any configuration:

```bash
# Run the complete 28-test verification suite
python tests/test_suite_all.py

# Run the 12-month backtester
python backtest.py
```
