# 🤖 Delta Exchange Automated Futures Trading Bot

A production-ready, Dockerized cryptocurrency futures trading bot for **Delta Exchange India (testnet)**. It trades **BTCUSDT perpetual futures** using an **EMA crossover + RSI confirmation** strategy, with full risk management, Telegram alerts, and PostgreSQL persistence.

---

## 📋 Table of Contents

1. [Prerequisites](#prerequisites)
2. [Getting Delta Exchange India Testnet API Keys](#getting-delta-exchange-india-testnet-api-keys)
3. [Creating a Telegram Bot](#creating-a-telegram-bot)
4. [Step-by-Step Setup (Windows)](#step-by-step-setup-windows)
5. [Running the Bot](#running-the-bot)
6. [Monitoring Logs](#monitoring-logs)
7. [Stopping the Bot Safely](#stopping-the-bot-safely)
8. [Environment Variables Reference](#environment-variables-reference)
9. [Trading Strategies](#trading-strategies)
10. [Risk Management Rules](#risk-management-rules)
11. [Architecture Overview](#architecture-overview)
12. [Live Dashboard](#live-dashboard)
13. [Kill Switch](#kill-switch)
14. [Troubleshooting](#troubleshooting)
15. [Changelog](#changelog)

---

## Prerequisites

Before you begin, make sure you have the following installed on your Windows machine:

| Software | Purpose | Download |
|----------|---------|----------|
| **Docker Desktop** | Runs the bot and PostgreSQL in containers | [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop) |
| **Git** | Clone and manage this repository | [git-scm.com/download/win](https://git-scm.com/download/win) |
| **Make** (optional) | Run convenience commands (`make up`, etc.) | Included with Git Bash, or install via `choco install make` |

> **Note:** If you don't have `make`, you can use the raw `docker-compose` commands shown in each section.

---

## Getting Delta Exchange India Testnet API Keys

1. Go to **[https://demo.delta.exchange](https://demo.delta.exchange)** (Delta Exchange India testnet) and create an account.
2. Complete any required verification (testnet usually doesn't require KYC).
3. Navigate to **Settings → API Keys** (or **Account → API Management**).
4. Click **Create API Key**.
5. Give it a name (e.g. "Trading Bot") and set permissions to **Trade** + **Read**.
6. Copy both the **API Key** and **API Secret** — you'll need them for the `.env` file.

> ⚠️ **Important:** These are TESTNET keys from **demo.delta.exchange** (India testnet). They work with simulated funds only. Never use mainnet keys with this bot until you've thoroughly tested it. The bot connects to the India testnet API at `https://cdn-ind.testnet.deltaex.org`.

Your testnet account comes pre-funded with **simulated USDT** so you can trade immediately.

---

## Creating a Telegram Bot

The bot sends real-time alerts (signals, fills, errors, PnL summaries) to your Telegram. Here's how to set it up:

### Step 1: Create a Telegram Bot

1. Open Telegram and search for **@BotFather**.
2. Send the command: `/newbot`
3. Follow the prompts:
   - Enter a **name** for your bot (e.g. "Delta Trading Alerts")
   - Enter a **username** (must end in `bot`, e.g. `delta_trader_alerts_bot`)
4. BotFather will reply with your **Bot Token** — it looks like:
   ```
   7123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
5. Copy this token — you'll need it for `TELEGRAM_BOT_TOKEN`.

### Step 2: Get Your Chat ID

1. **Send any message** to your new bot in Telegram (e.g. "hello").
2. Open this URL in your browser (replace `<YOUR_TOKEN>` with your actual token):
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
3. In the JSON response, find `"chat":{"id":` — this number is your **Chat ID**.
4. Copy this number — you'll need it for `TELEGRAM_CHAT_ID`.

> **Tip:** If the response is empty (`{"ok":true,"result":[]}`), send another message to your bot and refresh the URL.

---

## Step-by-Step Setup (Windows)

### 1. Clone or navigate to the repository

```powershell
cd C:\Users\YourName\Desktop\Trading_bot
```

### 2. Create your `.env` file

Copy the example and fill in your values:

```powershell
copy .env.example .env
```

Open `.env` in a text editor and fill in:
- `DELTA_API_KEY` — your India testnet API key (from demo.delta.exchange)
- `DELTA_API_SECRET` — your India testnet API secret
- `TELEGRAM_BOT_TOKEN` — from BotFather
- `TELEGRAM_CHAT_ID` — your chat ID
- `POSTGRES_PASSWORD` — choose a strong password
- Update `DATABASE_URL` to match your chosen password
- Set `ACTIVE_STRATEGY` to choose a trading strategy (default: `ema_crossover_rsi`). See [Trading Strategies](#trading-strategies) for available options.

### 3. Make sure Docker Desktop is running

Open Docker Desktop and wait until the Docker engine indicator shows "Running" (green).

### 4. Build and start the bot

Using Make:
```powershell
make build
make up
```

Or without Make (using docker-compose directly):
```powershell
docker-compose build
docker-compose up -d
```

### 5. Run database migrations

```powershell
make migrate
```

Or:
```powershell
docker exec -it delta_trading_bot alembic upgrade head
```

### 6. Verify it's running

```powershell
make logs
```

Or:
```powershell
docker-compose logs -f bot
```

You should see:
```
Delta Exchange Trading Bot — Starting Up
Initialising database…
Connecting to Delta Exchange India testnet…
Markets loaded: X symbols available.
Scheduler started — waiting for next 15m candle close…
```

You'll also receive a **🟢 Bot Started** message on Telegram.

---

## Running the Bot

| Command | Make | Docker Compose |
|---------|------|----------------|
| Build images | `make build` | `docker-compose build` |
| Start all services | `make up` | `docker-compose up -d` |
| Stop all services | `make down` | `docker-compose down` |
| View live logs | `make logs` | `docker-compose logs -f bot` |
| Open bot shell | `make shell` | `docker exec -it delta_trading_bot /bin/bash` |
| Run DB migrations | `make migrate` | `docker exec -it delta_trading_bot alembic upgrade head` |

---

## Monitoring Logs

### Live logs (Docker)
```powershell
make logs
```

### File logs
Logs are also written to the `logs/` directory on your host machine (mounted as a Docker volume):
```
logs/
├── bot.log          ← current day
├── bot.log.2025-01-01  ← previous days (rotated daily)
└── ...              ← kept for 30 days
```

### Log levels
Set `LOG_LEVEL` in `.env` to control verbosity:
- `DEBUG` — everything (very verbose, good for debugging)
- `INFO` — standard operation (recommended)
- `WARNING` — only warnings and errors
- `ERROR` — only errors
- `CRITICAL` — only critical failures

---

## Stopping the Bot Safely

### Normal shutdown
```powershell
make down
```

This sends SIGTERM to the bot, which triggers graceful shutdown:
1. ✅ Cancels all unfilled entry orders
2. ✅ Sends a "Bot Stopped" Telegram alert
3. ✅ Stops the scheduler
4. ✅ Open positions with SL/TP are **left on the exchange** (they'll still execute)

### Emergency shutdown (Kill Switch)
See the [Kill Switch](#kill-switch) section below.

---

## Environment Variables Reference

### General / Required

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DELTA_API_KEY` | Yes | — | Delta Exchange India testnet API key (from demo.delta.exchange) |
| `DELTA_API_SECRET` | Yes | — | Delta Exchange India testnet API secret |
| `TELEGRAM_BOT_TOKEN` | Yes | — | Telegram Bot API token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | — | Your Telegram chat ID for receiving alerts |
| `DATABASE_URL` | Yes | — | PostgreSQL connection string |
| `POSTGRES_USER` | Yes | `delta_bot` | PostgreSQL username |
| `POSTGRES_PASSWORD` | Yes | — | PostgreSQL password |
| `POSTGRES_DB` | Yes | `delta_trading` | PostgreSQL database name |
| `POSTGRES_HOST` | — | `db` | PostgreSQL host (Docker service name) |
| `POSTGRES_PORT` | — | `5432` | PostgreSQL port |

### Trading Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TRADING_SYMBOL` | `BTCUSD` | Trading pair symbol |
| `TIMEFRAME` | `15m` | Candle timeframe |
| `CAPITAL` | `10000` | Simulated starting capital in USDT |
| `RISK_PER_TRADE` | `0.02` | Fraction of capital risked per trade (2%) |
| `LEVERAGE` | `5` | Leverage multiplier |
| `STOP_LOSS_PCT` | `0.015` | Stop-loss distance from entry (1.5%) — global |
| `TAKE_PROFIT_PCT` | `0.03` | Take-profit distance from entry (3%) — global |
| `MAX_OPEN_POSITIONS` | `2` | Maximum concurrent open positions |
| `DAILY_LOSS_LIMIT` | `0.05` | Daily loss limit as fraction of capital (5%) |
| `MAKER_FEE` | `0.0002` | Maker fee rate (0.02%) |

### Strategy Selection

| Variable | Default | Description |
|----------|---------|-------------|
| `ACTIVE_STRATEGY` | `ema_crossover_rsi` | Which strategy to run. Options: `ema_crossover_rsi`, `bollinger_mean_revert`, `macd_momentum` |

### Strategy Parameters — EMA Crossover + RSI (`ema_crossover_rsi`)

| Variable | Default | Description |
|----------|---------|-------------|
| `EMA_FAST_PERIOD` | `9` | Fast EMA period |
| `EMA_SLOW_PERIOD` | `21` | Slow EMA period |
| `RSI_PERIOD` | `14` | RSI period |
| `RSI_BUY_LOW` | `40` | Minimum RSI for buy signals |
| `RSI_BUY_HIGH` | `65` | Maximum RSI for buy signals |
| `RSI_SELL_LOW` | `35` | Minimum RSI for sell signals |
| `RSI_SELL_HIGH` | `60` | Maximum RSI for sell signals |

### Strategy Parameters — Bollinger Band Mean Reversion (`bollinger_mean_revert`)

| Variable | Default | Description |
|----------|---------|-------------|
| `BB_PERIOD` | `20` | Bollinger Band lookback period |
| `BB_STD_DEV` | `2.0` | Number of standard deviations for bands |
| `BB_RSI_PERIOD` | `14` | RSI period for confirmation |
| `BB_RSI_OVERSOLD` | `30` | RSI oversold threshold (triggers buy) |
| `BB_RSI_OVERBOUGHT` | `70` | RSI overbought threshold (triggers sell) |

### Strategy Parameters — MACD Momentum (`macd_momentum`)

| Variable | Default | Description |
|----------|---------|-------------|
| `MACD_FAST` | `12` | Fast EMA period for MACD calculation |
| `MACD_SLOW` | `26` | Slow EMA period for MACD calculation |
| `MACD_SIGNAL` | `9` | Signal line smoothing period |

### Order Management / Logging / Other

| Variable | Default | Description |
|----------|---------|-------------|
| `ORDER_EXPIRY_CANDLES` | `3` | Cancel unfilled entries after N candles |
| `MAX_RETRIES` | `3` | API retry attempts on failure |
| `LOG_LEVEL` | `INFO` | Logging verbosity (DEBUG, INFO, WARNING, ERROR, CRITICAL) |
| `DASHBOARD_ENABLED` | `true` | Enable/disable the live dashboard |
| `DASHBOARD_PORT` | `8080` | Port the dashboard listens on |
| `KILL_SWITCH` | `false` | Emergency kill switch |

---

## Trading Strategies

The bot uses a **pluggable strategy system**. All strategies inherit from a common `BaseStrategy` abstract class, register themselves in a central registry, and are selected at runtime via the `ACTIVE_STRATEGY` environment variable. Strategy-specific parameters are configured through their own env vars.

### How It Works

1. **Base class** (`bot/strategies/base_strategy.py`) — defines the interface every strategy must implement: `evaluate()`, `get_required_candle_count()`, and `get_name()`.
2. **Registry** (`bot/strategies/registry.py`) — maps strategy name strings to their classes. On startup the bot looks up `ACTIVE_STRATEGY` in the registry and instantiates it.
3. **Env-based selection** — set `ACTIVE_STRATEGY=<name>` in your `.env` file and restart the bot. If the value is invalid, the bot exits with a clear error listing all available strategies.

### Switching Strategies

1. Open your `.env` file.
2. Change `ACTIVE_STRATEGY` to one of the available strategy names (see table below).
3. Configure the relevant strategy-specific env vars (if you want to override defaults).
4. Restart the bot: `make down && make up`.

### Available Strategies

| `ACTIVE_STRATEGY` value | Description | Env Vars |
|-------------------------|-------------|----------|
| `ema_crossover_rsi` (default) | EMA crossover with RSI confirmation. Buys when fast EMA crosses above slow EMA and RSI is in range; sells on the reverse. | `EMA_FAST_PERIOD`, `EMA_SLOW_PERIOD`, `RSI_PERIOD`, `RSI_BUY_LOW`, `RSI_BUY_HIGH`, `RSI_SELL_LOW`, `RSI_SELL_HIGH` |
| `bollinger_mean_revert` | Bollinger Band mean reversion. Buys when price closes below the lower band with RSI oversold; sells when price closes above the upper band with RSI overbought. | `BB_PERIOD`, `BB_STD_DEV`, `BB_RSI_PERIOD`, `BB_RSI_OVERSOLD`, `BB_RSI_OVERBOUGHT` |
| `macd_momentum` | MACD momentum crossover. Buys when MACD line crosses above the signal line with positive histogram; sells on the reverse. | `MACD_FAST`, `MACD_SLOW`, `MACD_SIGNAL` |

> **Note:** `STOP_LOSS_PCT` and `TAKE_PROFIT_PCT` are global and apply to all strategies.

### Strategy Details

#### EMA Crossover + RSI Confirmation (`ema_crossover_rsi`)

This is the original and default strategy. It combines two popular technical indicators:

- **EMA-9 (Fast):** Reacts quickly to price changes.
- **EMA-21 (Slow):** Represents the medium-term trend.
- **RSI-14:** Measures momentum on a 0-100 scale.

**Buy:** Fast EMA crosses above slow EMA AND RSI is between 40-65.
**Sell:** Fast EMA crosses below slow EMA AND RSI is between 35-60.

#### Bollinger Band Mean Reversion (`bollinger_mean_revert`)

Trades reversals when price touches the outer Bollinger Bands:

- **Bollinger Bands (20, 2.0):** Upper/lower bands at 2 standard deviations from the 20-period SMA.
- **RSI-14:** Confirms oversold/overbought conditions.

**Buy:** Price closes below the lower band AND RSI < 30 (oversold).
**Sell:** Price closes above the upper band AND RSI > 70 (overbought).

#### MACD Momentum (`macd_momentum`)

Trades momentum using MACD line / signal line crossovers:

- **MACD (12, 26, 9):** Difference between 12-period and 26-period EMA, smoothed with a 9-period signal line.

**Buy:** MACD line crosses above signal line AND histogram is positive.
**Sell:** MACD line crosses below signal line AND histogram is negative.

### Adding a Custom Strategy

1. **Create a new file** in `bot/strategies/` (e.g. `my_strategy.py`).
2. **Subclass `BaseStrategy`** and implement all required methods:
   ```python
   from bot.strategies.base_strategy import BaseStrategy, StrategySignal

   class MyStrategy(BaseStrategy):
       def __init__(self, config):
           # Read your strategy-specific env vars here
           ...

       def evaluate(self, candles):
           # Compute indicators, detect signals, return StrategySignal or None
           ...

       def get_required_candle_count(self) -> int:
           return 50  # how many candles your strategy needs

       def get_name(self) -> str:
           return "My Custom Strategy"
   ```
3. **Define strategy-specific env vars** — read them from `os.getenv()` in `__init__`. Use a unique prefix (e.g. `MY_FAST_PERIOD`).
4. **Register it** in `bot/strategies/registry.py`:
   ```python
   from bot.strategies.my_strategy import MyStrategy

   STRATEGY_REGISTRY = {
       ...
       "my_strategy": MyStrategy,
   }
   ```
5. **Add env vars** to `.env.example` and `docker-compose.yml`.
6. Set `ACTIVE_STRATEGY=my_strategy` in `.env` and restart.

---

## Risk Management Rules

### Position Sizing
- **Fixed fractional:** Each trade risks exactly **2% of capital** (200 USDT on the default 10,000 USDT capital).
- With **5x leverage**, the actual position value is 1,000 USDT per trade.
- Example: If BTC is at $50,000, position size = 1,000 / 50,000 = **0.02 BTC**.

### Stop-Loss & Take-Profit
- **Stop-loss:** Set **1.5% below entry** for longs, **1.5% above entry** for shorts.
- **Take-profit:** Set **3% above entry** for longs, **3% below entry** for shorts.
- This gives a **2:1 reward-to-risk ratio** — you only need to win 34% of trades to break even.

### Maximum Open Positions
- The bot can hold at most **2 positions** at any time.
- If 2 positions are already open and a new signal fires, it is skipped.

### Daily Loss Limit
- If the total daily PnL drops below **-5% of capital** (-500 USDT on 10,000 USDT), the bot **halts all new trading** and sends a Telegram alert.
- Existing SL/TP orders remain active (they'll still trigger on the exchange).
- Trading automatically **resumes at the next UTC midnight** (00:00 UTC).

### Order Expiry
- Entry orders are placed as **limit orders** at the mid-price (between bid and ask).
- If an entry order is not filled within **3 candles** (45 minutes), it is automatically cancelled.

### Fees
- The bot estimates **0.02% maker fee** per order and includes fees in PnL calculations.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Docker Compose                        │
│                                                          │
│  ┌─────────────────────┐    ┌──────────────────────┐    │
│  │     bot container    │    │    db container       │    │
│  │                      │    │                       │    │
│  │  ┌──────────────┐   │    │   PostgreSQL 15       │    │
│  │  │   main.py     │   │    │                       │    │
│  │  │  (scheduler)  │   │    │  Tables:              │    │
│  │  └──────┬────────┘   │    │  - trades             │    │
│  │         │             │    │  - orders             │    │
│  │  ┌──────▼────────┐   │    │  - signals            │    │
│  │  │  strategy.py   │   │    │  - pnl_summary        │    │
│  │  │  (EMA + RSI)   │   │    │                       │    │
│  │  └──────┬────────┘   │    └──────────────────────┘    │
│  │         │             │              ▲                  │
│  │  ┌──────▼────────┐   │              │                  │
│  │  │  executor.py   │───┼──────────────┘                  │
│  │  │  (order mgmt)  │   │                                 │
│  │  └──────┬────────┘   │                                 │
│  │         │             │                                 │
│  │  ┌──────▼────────┐   │     ┌──────────────────────┐   │
│  │  │  exchange.py   │───┼────▶│  Delta Exchange India  │   │
│  │  │  (REST client) │   │     │  Testnet API           │   │
│  │  └───────────────┘   │     └──────────────────────┘   │
│  │         │             │                                 │
│  │  ┌──────▼────────┐   │     ┌──────────────────────┐   │
│  │  │  risk.py       │   │     │  Telegram Bot API     │   │
│  │  │  (limits)      │   │     │  (alerts)             │   │
│  │  └───────────────┘   │     └──────────────────────┘   │
│  │                      │              ▲                  │
│  │  ┌───────────────┐   │              │                  │
│  │  │ telegram_alerts│───┼──────────────┘                  │
│  │  └───────────────┘   │                                 │
│  └─────────────────────┘                                  │
└─────────────────────────────────────────────────────────┘
```

### Module Responsibilities

| Module | Purpose |
|--------|---------|
| `main.py` | Entry point, scheduler, logging, graceful shutdown |
| `config.py` | Loads and validates all environment variables (incl. `ACTIVE_STRATEGY`) |
| `exchange.py` | Delta REST client wrapper for Delta Exchange India testnet |
| `strategies/` | Pluggable strategy system (base class, registry, strategy implementations) |
| `strategy.py` | Backward-compatible shim — re-exports from `strategies/` |
| `risk.py` | Position sizing, daily loss limit, kill switch |
| `executor.py` | Order placement, fill tracking, trade lifecycle |
| `telegram_alerts.py` | All Telegram notification functions |
| `database.py` | SQLAlchemy ORM models and session management |
| `pnl.py` | PnL calculation and 6-hour summary reports |

---

## Live Dashboard

The bot includes a real-time browser dashboard accessible at **http://localhost:8080** when the bot is running.

### Opening the Dashboard

Using Make:
```powershell
make dashboard
```

Or simply open **http://localhost:8080** in your browser after starting the bot with `make up`.

### Dashboard Panels

| Panel | Description |
|-------|-------------|
| **Bot Status Bar** | Live status indicator (Running/Halted/Stopped), UTC clock, uptime, kill switch button |
| **Live Market Ticker** | Real-time BTC/USDT mid-price, 24h change %, bid/ask spread (updates every 2s via WebSocket) |
| **Account Overview** | Starting capital, current balance, realized/unrealized/net PnL, daily PnL progress bar |
| **Strategy Monitor** | EMA 9/21 values, RSI 14, crossover state, next candle countdown, signal history table |
| **Statistics** | Win rate ring, total trades, avg win/loss, profit factor, Sharpe ratio, max drawdown |
| **Price Chart** | Candlestick-style BTCUSDT 15m chart with EMA overlays, entry markers, and SL/TP lines |
| **Open Positions** | Live table of open trades with entry/current price, unrealized PnL, SL/TP, duration |
| **Live Log Feed** | Last 50 log lines streamed in real time, color-coded by level, with search/filter |
| **PnL Performance** | Cumulative net PnL chart with green/red segments and zero line |
| **Trade History** | Paginated table of all closed trades with PnL, fees, exit reason, and duration |

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `DASHBOARD_ENABLED` | `true` | Set to `false` to disable the dashboard |
| `DASHBOARD_PORT` | `8080` | Port the dashboard listens on |

The dashboard runs as a FastAPI server in a background thread inside the bot container. It shares the same PostgreSQL database (read-only) and uses a thread-safe shared state object for real-time WebSocket data.

### Technology

- **Backend:** FastAPI + Uvicorn (ASGI)
- **Real-time:** WebSocket push every 2 seconds (no polling)
- **Frontend:** Single self-contained HTML file with vanilla JS + Chart.js
- **Theme:** Dark trading terminal aesthetic

---

## Kill Switch

The kill switch is an emergency mechanism to immediately stop all trading.

### How to activate

**Option 1: Environment variable**
Edit your `.env` file and set:
```
KILL_SWITCH=true
```
Then restart the bot:
```powershell
make down && make up
```

**Option 2: Docker environment update**
```powershell
docker exec -it delta_trading_bot env KILL_SWITCH=true
```
The bot checks the `KILL_SWITCH` env var on every 15m loop iteration.

### What happens when activated
1. ❌ All open orders are cancelled on the exchange
2. ❌ All open positions are closed at market (via aggressive limit orders)
3. 📝 All trades in DB are marked with `exit_reason = "kill_switch"`
4. 📱 A Telegram alert is sent
5. 🛑 The bot shuts down

### How to resume
1. Set `KILL_SWITCH=false` in `.env`
2. Restart: `make up`

---

## Troubleshooting

### Bot won't connect to Delta Exchange
- Make sure your India testnet API keys are correct (from **demo.delta.exchange**)
- Check that you're using **India testnet** keys (not mainnet or global testnet)
- Verify your internet connection inside Docker: `make shell` then `ping cdn-ind.testnet.deltaex.org`

### No signals being generated
- This is normal! EMA crossovers don't happen every candle.
- Check `LOG_LEVEL=DEBUG` for detailed indicator values each candle.
- On a 15m timeframe, you might see 2–5 signals per day depending on market conditions.

### Database connection errors
- Make sure the `db` container is healthy: `docker-compose ps`
- Verify `DATABASE_URL` matches your `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB`.
- Run migrations: `make migrate`

### Telegram alerts not working
- Verify you sent a message to your bot before trying to get the chat ID.
- Double-check both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
- Test manually: `curl "https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>&text=test"`

### Container keeps restarting
- Check logs: `docker-compose logs bot`
- Usually caused by missing/invalid environment variables — verify your `.env` file.

---

## 📜 License

This project is for educational and testing purposes only. Use at your own risk. Never trade with funds you cannot afford to lose.

---

## ⚠️ Disclaimer

This bot trades on **Delta Exchange India testnet** (demo.delta.exchange) with simulated funds. If you choose to use it on mainnet or any live exchange:
- You do so **entirely at your own risk**.
- Past performance and backtest results do **not** guarantee future returns.
- Cryptocurrency trading carries substantial risk of loss.
- The authors are **not** responsible for any financial losses.

---

## Changelog

### 2026-04-07 — Multi-Strategy Architecture

**Summary:** Added a pluggable strategy system that allows switching between multiple trading strategies via a single environment variable.

**New files created:**
| File | Purpose |
|------|---------|
| `bot/strategies/__init__.py` | Package init — exports `BaseStrategy`, `get_strategy`, `STRATEGY_REGISTRY` |
| `bot/strategies/base_strategy.py` | Abstract base class and `StrategySignal` dataclass |
| `bot/strategies/ema_crossover_rsi.py` | EMA Crossover + RSI strategy (migrated from `bot/strategy.py`) |
| `bot/strategies/bollinger_mean_revert.py` | Bollinger Band mean reversion strategy |
| `bot/strategies/macd_momentum.py` | MACD momentum crossover strategy |
| `bot/strategies/registry.py` | Strategy name-to-class mapping and `get_strategy()` factory |

**Modified files:**
| File | Change |
|------|--------|
| `bot/config.py` | Added `active_strategy` field (reads `ACTIVE_STRATEGY` env var, defaults to `ema_crossover_rsi`) |
| `bot/main.py` | Instantiates strategy from registry on startup; uses `strategy.evaluate()` and `strategy.get_required_candle_count()` instead of direct function calls; logs active strategy name |
| `bot/strategy.py` | Converted to a backward-compatible shim that re-exports `StrategySignal`, `compute_indicators`, `detect_crossover`, and `generate_signal` from the new `strategies/` package |
| `.env` | Added `ACTIVE_STRATEGY=ema_crossover_rsi` |
| `.env.example` | Added `ACTIVE_STRATEGY` and commented-out env vars for Bollinger and MACD strategies |
| `docker-compose.yml` | Added `ACTIVE_STRATEGY` to bot service environment |
| `README.md` | Added Trading Strategies section, updated Environment Variables Reference, updated Architecture table, added this Changelog |

**Relocated code:**
- Strategy logic (EMA crossover + RSI indicator computation, crossover detection, RSI filtering) moved from `bot/strategy.py` into `bot/strategies/ema_crossover_rsi.py` as the `EMACrossoverRSI` class. The original `bot/strategy.py` remains as a shim for backward compatibility with `bot/executor.py` imports.
