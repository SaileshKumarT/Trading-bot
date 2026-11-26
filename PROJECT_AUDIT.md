# Delta Exchange Trading Bot - Complete Project Audit

**Date:** 2026-11-26
**Codebase:** 35 files, ~8,700 lines of code
**Language:** Python 3.12
**Status:** Functional, deployed via Docker on Delta Exchange India Testnet

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture Summary](#2-architecture-summary)
3. [File Structure](#3-file-structure)
4. [Core Modules](#4-core-modules)
5. [Trading Strategies](#5-trading-strategies)
6. [Risk Management](#6-risk-management)
7. [PnL Tracking](#7-pnl-tracking)
8. [Database Schema](#8-database-schema)
9. [Dashboard & UI](#9-dashboard--ui)
10. [Telegram Alerts](#10-telegram-alerts)
11. [WebSocket Client](#11-websocket-client)
12. [Infrastructure & Deployment](#12-infrastructure--deployment)
13. [Configuration Reference](#13-configuration-reference)
14. [Dependencies](#14-dependencies)
15. [Setup from Scratch](#15-setup-from-scratch)
16. [Known Issues & Observations](#16-known-issues--observations)

---

## 1. Project Overview

A synchronous, scheduler-driven cryptocurrency perpetual-futures trading bot targeting the **Delta Exchange India Testnet**. It trades a single symbol (default: BTCUSD) on configurable timeframes using a pluggable strategy system with limit-order entries and stop-loss/take-profit bracket exits.

**Key capabilities:**
- 3 pluggable trading strategies (EMA Crossover+RSI, Bollinger Mean Revert, MACD Momentum)
- Real-time WebSocket market data streaming from Delta Exchange
- PostgreSQL database with Alembic migrations for trade/order/signal persistence
- Risk management (position limits, daily loss limits, kill switch, SL/TP)
- Real-time web dashboard with TradingView charts, WebSocket live updates
- Telegram notifications for every trade lifecycle event
- Docker Compose deployment (bot + PostgreSQL)
- Full trade reconciliation and orphan cleanup on every candle tick

---

## 2. Architecture Summary

```
                    ┌─────────────────────────────────────────────┐
                    │              Delta Exchange API              │
                    │         (REST + WebSocket Testnet)           │
                    └──────────┬──────────────┬───────────────────┘
                               │ REST         │ WebSocket
                               ▼              ▼
                    ┌──────────────┐  ┌───────────────┐
                    │  Exchange    │  │  ws_client.py  │
                    │ (REST API)  │  │ (market data)  │
                    └──────┬───────┘  └───────────────┘
                           │
                           ▼
┌────────┐    ┌─────────────────────────┐    ┌──────────────┐
│Config  │───▶│      main.py            │───▶│  Telegram     │
│(frozen │    │  (APScheduler loop)     │    │  Alerts       │
│dataclass)   │                         │    └──────────────┘
└────────┘    │  strategy_loop():       │
              │   1. Kill switch check  │    ┌──────────────┐
              │   2. Reconcile pos/ord  │───▶│  Dashboard    │
              │   3. Check pending fills│    │  (FastAPI +   │
              │   4. Check SL/TP fills  │    │   WebSocket)  │
              │   5. Cleanup orphans    │    └──────────────┘
              │   6. Evaluate strategy  │
              │   7. Execute signal     │    ┌──────────────┐
              │   8. Update dashboard   │───▶│  PostgreSQL   │
              └───────────┬─────────────┘    │  (via Docker) │
                          │                  └──────────────┘
                          ▼
              ┌─────────────────────┐
              │     Executor        │
              │  (trade lifecycle)  │
              │  + RiskManager      │
              │  + Strategy         │
              └─────────────────────┘
```

**Execution model:** Single-process, synchronous. APScheduler's `BlockingScheduler` fires the strategy loop at candle-close boundaries (+ 5-second buffer). The FastAPI dashboard runs in a background daemon thread. No asyncio in the main trading path.

---

## 3. File Structure

```
Trading_bot/
├── .env.example              # All configuration with documentation
├── .gitignore                # Excludes .env, logs, pycache, IDE files
├── Dockerfile                # Python 3.12-slim, single-stage build
├── Makefile                  # build/up/down/logs/shell/migrate/dashboard
├── README.md                 # Project readme
├── requirements.txt          # 12 Python dependencies
├── docker-compose.yml        # bot + PostgreSQL services
├── alembic.ini               # Alembic migration config
├── alembic/
│   ├── env.py                # Migration environment (loads .env, sets metadata)
│   ├── script.py.mako        # Migration template
│   └── versions/
│       ├── 2025_01_01_..._initial_schema.py    # Creates 4 tables
│       └── 2025_03_31_..._add_sl_tp_order_ids.py  # Adds SL/TP order ID columns
├── bot/
│   ├── __init__.py           # Package init (docstring only)
│   ├── config.py             # Frozen Config dataclass, env var loading
│   ├── main.py               # Entry point, scheduler, strategy loop (~608 lines)
│   ├── exchange.py           # Delta Exchange REST API wrapper (~835 lines)
│   ├── executor.py           # Trade lifecycle engine (~1550 lines)
│   ├── risk.py               # Risk management (sizing, SL/TP, daily limits)
│   ├── pnl.py                # PnL calculation and periodic summaries
│   ├── strategy.py           # Backward-compat shim, re-exports from strategies/
│   ├── database.py           # SQLAlchemy ORM models (4 tables)
│   ├── telegram_alerts.py    # Telegram Bot API notifications (~245 lines)
│   ├── ws_client.py          # Exchange WebSocket client (~482 lines)
│   ├── strategies/
│   │   ├── __init__.py       # Exports BaseStrategy, get_strategy, STRATEGY_REGISTRY
│   │   ├── base_strategy.py  # ABC + StrategySignal dataclass
│   │   ├── registry.py       # Strategy name -> class mapping
│   │   ├── ema_crossover_rsi.py      # Strategy 1: EMA crossover with RSI filter
│   │   ├── bollinger_mean_revert.py  # Strategy 2: Bollinger band mean reversion
│   │   └── macd_momentum.py          # Strategy 3: MACD momentum crossover
│   └── dashboard/
│       ├── __init__.py       # Package init
│       ├── server.py         # FastAPI server, REST + WebSocket endpoints (~557 lines)
│       ├── state.py          # Thread-safe SharedState singleton (~307 lines)
│       ├── ws_manager.py     # WebSocket connection manager (~115 lines)
│       └── static/
│           └── index.html    # Full SPA dashboard (~1397 lines, HTML+CSS+JS)
└── logs/
    └── .gitkeep              # Keeps logs/ in git
```

---

## 4. Core Modules

### 4.1 config.py - Configuration Management

A frozen (immutable) dataclass singleton loaded from environment variables via `python-dotenv`. Fail-fast design: missing required vars (`DELTA_API_KEY`, `DELTA_API_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DATABASE_URL`) cause `sys.exit(1)`.

Imported everywhere as `from bot.config import cfg`.

Helper functions: `_require(key)`, `_get_float(key, default)`, `_get_int(key, default)`, `_get_bool(key, default)`.

### 4.2 exchange.py - Exchange Client

A pure-REST wrapper for Delta Exchange India Testnet v2 API using `requests` with HMAC-SHA256 authentication. Intentionally avoids CCXT but returns CCXT-compatible dict structures for portability.

**Key methods:**
- `_sign(method, path, body)` - HMAC-SHA256 auth headers
- `_request(method, path, params, payload, auth)` - HTTP transport with JSON parsing
- `_retry(func, *args)` - Exponential backoff (2^n seconds) on ConnectionError, Timeout, 429, 5xx. Does NOT retry 4xx.
- `load_markets()` - Fetches `/v2/products`, populates product caches
- `fetch_ohlcv(symbol, timeframe, limit)` - OHLCV candles as `[ts_ms, O, H, L, C, V]`
- `place_limit_order(symbol, side, amount, price)` - Limit entry at mid-price
- `place_stop_order(symbol, side, amount, stop_price, type)` - SL/TP as stop-limit, always `reduce_only=True`
- `cancel_order(order_id)` / `cancel_all_orders(symbol)` - Order cancellation
- `fetch_positions(symbols)` - Derives side from sign of size (Delta API lacks a side field)
- `close_position(symbol, side, amount)` - Reduce-only limit near mid-price
- `set_leverage(leverage, symbol)` - Set product leverage

**Symbol resolution:** Handles CCXT-style `BTC/USDT:USDT`, plain `BTCUSD`, and `BTCUSDT` -> `BTCUSD` fallback.

**Contract conversion:** `_amount_to_contracts()` converts BTC amounts to whole contracts (e.g., 1 contract = 0.001 BTC), minimum 1.

### 4.3 executor.py - Trade Execution Engine

The largest module (~1550 lines). Orchestrates the full trade lifecycle.

**Primary methods (called from strategy_loop):**
- `process_signal(signal)` - Entry: logs signal to DB, runs risk checks, spread filter, calculates position size, places limit entry at mid-price, starts candle-expiry counter
- `check_pending_entries()` - Polls pending entries: if filled -> `_handle_entry_fill` (places SL/TP brackets). If stale -> cancel after `ORDER_EXPIRY_CANDLES` (default 3)
- `check_open_trades()` - Polls SL/TP orders: if filled -> `_handle_exit_fill` (calculates PnL, cancels counterpart)
- `cleanup_orphaned_orders()` - If no exchange position: cancel ALL exchange orders, mark stale DB trades as failed. If position exists: cancel only unlinked orders
- `cleanup_stale_db_orders()` - Marks DB orders as cancelled if gone from exchange
- `reconcile_positions_and_orders()` - Ensures exactly 1 SL + 1 TP per open position. Cancels and replaces when inconsistent. Returns False if it had to fix things (caller skips signal evaluation)
- `execute_kill_switch()` - Emergency: cancels all orders, closes all positions, marks trades as "killed"
- `reload_state()` - On startup: re-populates pending entry counters, runs orphan cleanup

**Design patterns:**
- Entry orders are **limit orders at mid-price** (not market) for slippage control
- SL/TP are **stop-limit orders** (not stop-market) for price certainty
- **Counterpart cancel pattern**: When SL fills, immediately cancel TP using stored `tp_order_id` (and vice versa)
- **Belt-and-braces cancellation**: Individual cancels first, `cancel_all_orders` as fallback

### 4.4 main.py - Entry Point & Main Loop

**Startup sequence (`main()`):**
1. Configure logging (console + rotating file + dashboard handler)
2. Select and instantiate active strategy via registry
3. Initialize database (SQLAlchemy engine + create tables)
4. Create Exchange, load markets, set leverage
5. Create RiskManager, reload daily PnL from DB
6. Create Executor, reload state (re-track pending entries, orphan cleanup)
7. Sync orphaned DB positions
8. Check kill switch (exit immediately if active)
9. Register SIGINT/SIGTERM handlers
10. Start FastAPI dashboard in background thread
11. Send Telegram "bot started" alert
12. Start APScheduler (BlockingScheduler, blocks here)

**Strategy loop (every candle close):**
1. Kill switch check (re-reads env var)
2. Daily loss halt check
3. Reconcile positions and orders
4. Check pending entry fills/expiry
5. Check SL/TP fills on open trades
6. Cleanup orphaned and stale orders
7. Fetch candles + evaluate strategy signal
8. Process signal through executor
9. Update dashboard shared state (always, in `finally` block)

**Scheduler:**
- Strategy loop: CronTrigger aligned to candle boundaries + 5s buffer (e.g., for 15m: fires at :00, :15, :30, :45 + 5s)
- PnL summary: Every 6 hours at 00:01, 06:01, 12:01, 18:01 UTC

**Graceful shutdown (SIGINT/SIGTERM):**
Cancel all exchange orders -> close all positions -> mark open trades as "bot_shutdown" -> Telegram alert -> scheduler.shutdown() -> sys.exit(0)

### 4.5 strategy.py - Backward-Compatible Shim

Re-exports `StrategySignal`, `compute_indicators`, and `detect_crossover` from the strategies package. Provides `generate_signal(candles)` via a singleton `EMACrossoverRSI`. The executor imports `StrategySignal` from here.

---

## 5. Trading Strategies

### Strategy Architecture

Pluggable system built on `BaseStrategy` ABC with a registry pattern.

**Base class** (`strategies/base_strategy.py`):
- Abstract methods: `evaluate(candles) -> StrategySignal | None`, `get_required_candle_count() -> int`, `get_name() -> str`
- `StrategySignal` dataclass: `signal_type` ("buy"/"sell"), `price`, `ema_fast`, `ema_slow`, `rsi`

**Registry** (`strategies/registry.py`): Maps string names to classes. `get_strategy(name, config)` instantiates or exits with fatal error.

### 5.1 EMA Crossover + RSI Confirmation

**File:** `strategies/ema_crossover_rsi.py`

**Indicators:** EMA-9, EMA-21, RSI-14 (all via `pandas_ta`)

**Parameters (from Config dataclass):**

| Parameter | Env Var | Default |
|-----------|---------|---------|
| Fast EMA period | `EMA_FAST_PERIOD` | 9 |
| Slow EMA period | `EMA_SLOW_PERIOD` | 21 |
| RSI period | `RSI_PERIOD` | 14 |
| RSI buy floor/ceiling | `RSI_BUY_LOW` / `RSI_BUY_HIGH` | 40 / 65 |
| RSI sell floor/ceiling | `RSI_SELL_LOW` / `RSI_SELL_HIGH` | 35 / 60 |

**Signal logic** (evaluated on last closed candle `iloc[-2]` vs `iloc[-3]`):

**BUY:** `prev_ema_fast <= prev_ema_slow AND curr_ema_fast > curr_ema_slow AND 40 <= RSI <= 65`
**SELL:** `prev_ema_fast >= prev_ema_slow AND curr_ema_fast < curr_ema_slow AND 35 <= RSI <= 60`

Minimum candles: `ema_slow_period + 10` (default: 31)

### 5.2 Bollinger Band Mean Reversion

**File:** `strategies/bollinger_mean_revert.py`

**Indicators:** Bollinger Bands (20-period SMA +/- 2 stddev), RSI-14

**Parameters (read directly from env vars, NOT Config dataclass):**

| Parameter | Env Var | Default |
|-----------|---------|---------|
| BB period | `BB_PERIOD` | 20 |
| BB std dev multiplier | `BB_STD_DEV` | 2.0 |
| RSI period | `BB_RSI_PERIOD` | 14 |
| RSI oversold | `BB_RSI_OVERSOLD` | 30 |
| RSI overbought | `BB_RSI_OVERBOUGHT` | 70 |

**Signal logic** (evaluated on `iloc[-2]`):

**BUY:** `close <= BB_lower AND RSI < 30`
**SELL:** `close >= BB_upper AND RSI > 70`

**StrategySignal field reuse:** `ema_fast` carries `bb_lower`/`bb_upper`, `ema_slow` carries `bb_mid`

Minimum candles: `bb_period + 10` (default: 30)

### 5.3 MACD Momentum

**File:** `strategies/macd_momentum.py`

**Indicators:** MACD Line (EMA-12 - EMA-26), Signal Line (EMA-9 of MACD), Histogram

**Parameters (read directly from env vars, NOT Config dataclass):**

| Parameter | Env Var | Default |
|-----------|---------|---------|
| MACD fast | `MACD_FAST` | 12 |
| MACD slow | `MACD_SLOW` | 26 |
| MACD signal | `MACD_SIGNAL` | 9 |

**Signal logic** (comparing `iloc[-3]` with `iloc[-2]`):

**BUY:** `prev_macd <= prev_signal AND curr_macd > curr_signal AND histogram > 0`
**SELL:** `prev_macd >= prev_signal AND curr_macd < curr_signal AND histogram < 0`

**StrategySignal field reuse:** `ema_fast` = MACD, `ema_slow` = signal, `rsi` = histogram

Minimum candles: `slow + signal + 10` (default: 45)

---

## 6. Risk Management

**File:** `bot/risk.py` - `RiskManager` class

### 6.1 Position Sizing (Fixed-Fractional)

```
notional       = capital * risk_per_trade       # 10000 * 0.02 = 200 USDT
position_value = notional * leverage            # 200 * 5 = 1000 USDT
qty            = position_value / entry_price   # 1000 / 95000 = 0.010526 BTC
```

### 6.2 Stop-Loss / Take-Profit

| | Long (buy) | Short (sell) |
|---|---|---|
| Stop-Loss | `entry * (1 - 0.015)` | `entry * (1 + 0.015)` |
| Take-Profit | `entry * (1 + 0.03)` | `entry * (1 - 0.03)` |

Risk-reward ratio: 1:2 (1.5% risk vs 3% reward)

### 6.3 Position Limit Check (`can_open_new_position()`)

Three checks in order:
1. **Kill switch** - Re-reads `KILL_SWITCH` env var on every call
2. **Daily loss halt** - If `_trading_halted` is True, block
3. **Max positions** - Counts DB trades with status "open" or "pending" vs `MAX_OPEN_POSITIONS` (default: 2)

### 6.4 Daily Loss Limit

- Limit: `-(capital * daily_loss_limit)` = -500 USDT (default)
- If `daily_pnl <= -500`: `_trading_halted = True`, all new positions blocked
- Resets at 00:00 UTC daily
- `reload_daily_pnl_from_db()` restores the counter on restart

### 6.5 Fee Estimation

```
fee = qty * price * maker_fee    # maker_fee = 0.0002 (0.02%)
```

---

## 7. PnL Tracking

**File:** `bot/pnl.py`

### Trade PnL Calculation

- **Long:** `pnl = (exit_price - entry_price) * qty`
- **Short:** `pnl = (entry_price - exit_price) * qty`

Note: `leverage` parameter is accepted but unused (already factored into qty at entry).

### Periodic Summary (every 6 hours)

Computes: total trades, wins, losses, gross PnL, net PnL, fees, win rate, best/worst trade, open positions. Writes a `PnlSummary` row to DB and sends via Telegram.

### No Equity Curve Tracking

There is no historical high-water mark, peak-to-trough drawdown, or equity curve. The daily PnL in RiskManager acts as a rudimentary intraday drawdown guard.

---

## 8. Database Schema

**Engine:** PostgreSQL 15, SQLAlchemy 2.x ORM, pool_size=5, max_overflow=10, pool_pre_ping=True

### Table: `trades`

| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | Auto-increment |
| symbol | String(50) | e.g. "BTCUSD" |
| side | String(10) | "buy" or "sell" |
| entry_price | Float | Filled on entry fill |
| exit_price | Float | Filled on exit |
| qty | Float | Position size |
| leverage | Integer | Leverage multiplier |
| pnl | Float | Computed after exit |
| fees | Float | Default 0.0 |
| status | String(20) | "pending" -> "open" -> "closed"/"failed"/"killed" |
| entry_time | DateTime(tz) | |
| exit_time | DateTime(tz) | |
| exit_reason | String(50) | "stop_loss", "take_profit", "signal", "bot_shutdown", "orphan_sync" |
| sl_order_id | String(100) | Exchange SL order ID (migration 0002) |
| tp_order_id | String(100) | Exchange TP order ID (migration 0002) |

### Table: `orders`

| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| trade_id | Integer FK -> trades.id | |
| exchange_order_id | String(100) | |
| type | String(20) | "limit", "stop_limit" |
| side | String(10) | |
| price | Float | |
| qty | Float | |
| status | String(20) | Default "pending" |
| created_at | DateTime(tz) | Auto |
| updated_at | DateTime(tz) | Auto on update |

### Table: `signals`

| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| symbol | String(50) | |
| timeframe | String(10) | |
| signal_type | String(10) | "buy" or "sell" |
| ema_fast | Float | |
| ema_slow | Float | |
| rsi | Float | |
| price | Float | |
| timestamp | DateTime(tz) | Auto |

### Table: `pnl_summary`

| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| period_start / period_end | DateTime(tz) | |
| total_trades, wins, losses | Integer | Default 0 |
| gross_pnl, net_pnl, fees_paid | Float | Default 0 |
| generated_at | DateTime(tz) | Auto |

### Alembic Migrations

- **0001 (2025-01-01):** Creates all 4 tables
- **0002 (2025-03-31):** Adds `sl_order_id` and `tp_order_id` to trades table

---

## 9. Dashboard & UI

### Server Architecture

**File:** `bot/dashboard/server.py` - FastAPI application, started in a daemon thread via `start_dashboard_in_thread()` running Uvicorn on `0.0.0.0:8080`.

### REST API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Serves the SPA HTML |
| GET | `/api/status` | Bot status, uptime, kill switch state |
| GET | `/api/account` | Balance, realized/unrealized/daily PnL, fees |
| GET | `/api/positions` | Open positions with live unrealized PnL, SL/TP |
| GET | `/api/trades` | Paginated closed trade history (max 100/page) |
| GET | `/api/signals` | Last 10 strategy signals |
| GET | `/api/stats` | Win rate, profit factor, Sharpe ratio, max drawdown |
| GET | `/api/candles` | OHLCV data with EMA overlays |
| GET | `/api/pnl_chart` | Cumulative PnL curve data |
| POST | `/api/kill` | Kill switch (requires `X-Kill-Confirm: yes` header) |

### WebSocket Endpoints

| Path | Purpose | Interval |
|------|---------|----------|
| `/ws/live` | Full live-state snapshot | Every 2 seconds |
| `/ws/logs` | Real-time bot log stream | Every 1 second |

### Frontend UI Panels (index.html ~1397 lines)

Dark-themed trading terminal, no CSS framework. External libraries:
- **Chart.js 4.4.1** (cumulative PnL line chart)
- **TradingView Lightweight Charts 4.1.1** (candlestick + EMA overlays)

**Layout (10 panels):**
1. **Bot Status Bar** - Running/halted/stopped indicator, UTC clock, uptime, exchange info, Kill Switch button
2. **Live Market Ticker** - Mid price, 24h change, bid/ask/spread
3. **Account Overview** - Capital, balance, unrealized/realized/net PnL, daily loss progress bar
4. **Strategy Monitor** - Next candle countdown, EMA/RSI values, crossover state, signal history
5. **Statistics** - Win rate ring, profit factor, Sharpe ratio, max drawdown, avg duration
6. **Price Chart** - TradingView candlestick (500px), EMA-9 (blue) + EMA-21 (orange) overlays, entry markers, SL/TP dashed lines
7. **Open Positions** - Table with entry/current/PnL/SL/TP/duration
8. **Live Log Feed** - Color-coded scrollable log panel with search/filter
9. **Cumulative PnL Chart** - Equity curve (green above zero, red below)
10. **Trade History** - Paginated closed trades table with green/red row shading

Responsive layout: multi-column rows collapse to single-column under 768px.

### State Management

**SharedState** (`dashboard/state.py`): Thread-safe singleton, all fields protected by `threading.Lock`. Updated exclusively by `_update_dashboard_state()` in main.py at each candle close.

**Data freshness note:** Dashboard data is only as fresh as the last strategy loop execution. Between candle closes (e.g., 15 min), prices and indicators are frozen. The exchange WebSocket client runs continuously but its `MarketState` is NOT wired into the dashboard's SharedState.

### Kill Switch

Three activation paths:
1. **Dashboard UI** - Red button -> `POST /api/kill` with `X-Kill-Confirm: yes` header (JS confirm dialog)
2. **Environment variable** - `KILL_SWITCH=true` checked every strategy loop iteration
3. **Startup check** - main() checks before starting scheduler

---

## 10. Telegram Alerts

**File:** `bot/telegram_alerts.py` - Synchronous `requests.post` to Telegram Bot API with Markdown formatting.

| Alert | Trigger |
|-------|---------|
| `alert_bot_started()` | After all initialization |
| `alert_bot_stopped()` | On graceful shutdown |
| `alert_signal_detected()` | New trading signal fires |
| `alert_order_placed()` | Order submitted to exchange |
| `alert_order_filled()` | Entry order fills |
| `alert_order_cancelled()` | Order cancelled |
| `alert_stop_loss_triggered()` | SL exits a trade |
| `alert_take_profit_triggered()` | TP exits a trade |
| `alert_daily_loss_limit()` | Daily loss limit breached |
| `alert_kill_switch()` | Kill switch activates |
| `alert_error()` | Unhandled exception (truncated 500 chars) |
| `alert_pnl_summary()` | Every 6 hours (00:01, 06:01, 12:01, 18:01 UTC) |

---

## 11. WebSocket Client

**File:** `bot/ws_client.py`

Persistent WebSocket connection to `wss://testnet-socket.delta.exchange` with HMAC-SHA256 authentication.

**Subscribed channels:**

| Channel | Type | Data |
|---------|------|------|
| `l2_orderbook` | Public | Best bid/ask/mid prices |
| `candlestick_{timeframe}` | Public | Live OHLCV candle updates |
| `mark_price` | Public | Mark price |
| `v2/ticker` | Public | Ticker data |
| `positions` | Private | User position changes |
| `orders` | Private | User order changes |
| `margins` | Private | Margin updates |

**Features:**
- Heartbeat ping every 25 seconds
- Exponential backoff reconnection (1s base, 60s max)
- `MarketState` dataclass stores latest data
- `asyncio.Event` for graceful shutdown
- Standalone test mode: `python -m bot.ws_client`

**Note:** `MarketState` is NOT thread-safe (no lock) and is NOT wired into the dashboard's SharedState.

---

## 12. Infrastructure & Deployment

### Docker Architecture

```
Host Machine
  |
  +-- bot_network (bridge)
  |     |
  |     +-- delta_bot_db (postgres:15)
  |     |     Internal: 5432 (not exposed to host)
  |     |     Volume: pgdata -> /var/lib/postgresql/data
  |     |
  |     +-- delta_trading_bot (python:3.12-slim)
  |           Exposed: 8080 (dashboard)
  |           Volume: ./logs -> /app/logs
  |           Connects to: db:5432
```

### Dockerfile

- Base: `python:3.12-slim`
- System deps: `gcc`, `libpq-dev` (for psycopg2 compilation)
- Pip install with `--no-cache-dir` (layer cached when deps unchanged)
- CMD: `python -m bot.main`

### docker-compose.yml (v3.9)

- **db service**: postgres:15, healthcheck via `pg_isready`, named volume `pgdata`
- **bot service**: builds from Dockerfile, depends on db (healthy), reads `.env`, maps dashboard port

### Makefile Targets

| Target | Command | Purpose |
|--------|---------|---------|
| `build` | `docker-compose build` | Build bot image |
| `up` | `docker-compose up -d` | Start services detached |
| `down` | `docker-compose down` | Stop services (volumes preserved) |
| `logs` | `docker-compose logs -f bot` | Tail bot logs |
| `shell` | `docker exec -it delta_trading_bot /bin/bash` | Shell into bot container |
| `migrate` | `docker exec -it delta_trading_bot alembic upgrade head` | Run migrations |
| `dashboard` | Opens `http://localhost:8080` | Open dashboard in browser |

---

## 13. Configuration Reference

### Required (fatal on absence)

| Variable | Purpose |
|----------|---------|
| `DELTA_API_KEY` | Delta Exchange API key |
| `DELTA_API_SECRET` | Delta Exchange API secret |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Telegram chat ID for alerts |
| `DATABASE_URL` | PostgreSQL connection string |

### Trading Parameters

| Variable | Default | Purpose |
|----------|---------|---------|
| `DELTA_BASE_URL` | `https://cdn-ind.testnet.deltaex.org` | Exchange API base URL |
| `TRADING_SYMBOL` | `BTCUSD` | Perpetual to trade |
| `TIMEFRAME` | `15m` | Candle interval |
| `CAPITAL` | `10000` | Starting capital (USDT) |
| `RISK_PER_TRADE` | `0.02` | Fraction risked per trade (2%) |
| `LEVERAGE` | `5` | Leverage multiplier |
| `STOP_LOSS_PCT` | `0.015` | Stop-loss distance (1.5%) |
| `TAKE_PROFIT_PCT` | `0.03` | Take-profit distance (3%) |
| `MAX_OPEN_POSITIONS` | `2` | Concurrent position cap |
| `DAILY_LOSS_LIMIT` | `0.05` | Daily loss halt (5% of capital) |
| `MAKER_FEE` | `0.0002` | Fee rate (0.02%) |
| `ACTIVE_STRATEGY` | `ema_crossover_rsi` | Strategy key |
| `ORDER_EXPIRY_CANDLES` | `3` | Candles before unfilled entry cancelled |
| `MAX_RETRIES` | `3` | API retry attempts |
| `SPREAD_MAX_PCT` | `0.005` | Max bid-ask spread filter |

### Strategy Parameters (EMA Crossover + RSI)

| Variable | Default |
|----------|---------|
| `EMA_FAST_PERIOD` | `9` |
| `EMA_SLOW_PERIOD` | `21` |
| `RSI_PERIOD` | `14` |
| `RSI_BUY_LOW` / `RSI_BUY_HIGH` | `40` / `65` |
| `RSI_SELL_LOW` / `RSI_SELL_HIGH` | `35` / `60` |

### Strategy Parameters (Bollinger - read from env directly)

| Variable | Default |
|----------|---------|
| `BB_PERIOD` | `20` |
| `BB_STD_DEV` | `2.0` |
| `BB_RSI_PERIOD` | `14` |
| `BB_RSI_OVERSOLD` / `BB_RSI_OVERBOUGHT` | `30` / `70` |

### Strategy Parameters (MACD - read from env directly)

| Variable | Default |
|----------|---------|
| `MACD_FAST` | `12` |
| `MACD_SLOW` | `26` |
| `MACD_SIGNAL` | `9` |

### System

| Variable | Default | Purpose |
|----------|---------|---------|
| `LOG_LEVEL` | `INFO` | Python log level |
| `KILL_SWITCH` | `false` | Emergency stop (runtime-readable) |
| `DASHBOARD_ENABLED` | `true` | Enable web dashboard |
| `DASHBOARD_PORT` | `8080` | Dashboard HTTP port |
| `POSTGRES_USER` | `delta_bot` | DB user |
| `POSTGRES_PASSWORD` | (must change) | DB password |
| `POSTGRES_DB` | `delta_trading` | DB name |
| `POSTGRES_HOST` | `db` | DB host (Docker service name) |
| `POSTGRES_PORT` | `5432` | DB port |

---

## 14. Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| requests | >=2.31.0 | HTTP client for Delta Exchange REST API and Telegram |
| pandas | >=2.1.0 | DataFrame operations for indicator calculations |
| pandas-ta | ==0.4.71b0 | Technical analysis (EMA, RSI, MACD, Bollinger Bands) |
| python-telegram-bot | >=20.7 | Listed but unused (alerts use raw `requests`) |
| SQLAlchemy | >=2.0.23 | ORM for PostgreSQL |
| alembic | >=1.13.0 | Database migrations |
| psycopg2-binary | >=2.9.9 | PostgreSQL adapter |
| APScheduler | >=3.10.4 | Candle-aligned job scheduling |
| python-dotenv | >=1.0.0 | .env file loading |
| fastapi | >=0.109.0 | Dashboard HTTP + WebSocket server |
| uvicorn | >=0.27.0 | ASGI server for FastAPI |
| websockets | >=12.0 | WebSocket support for dashboard |

---

## 15. Setup from Scratch

**Prerequisites:** Docker, Docker Compose, Git

1. **Clone:** `git clone <repo-url> && cd Trading_bot`
2. **Configure:** `cp .env.example .env` and edit with real values:
   - `DELTA_API_KEY` / `DELTA_API_SECRET` (from https://testnet.delta.exchange)
   - `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` (from @BotFather)
   - `POSTGRES_PASSWORD` (change from default)
   - `DATABASE_URL` (update password to match)
   - `ACTIVE_STRATEGY` (choose: `ema_crossover_rsi`, `bollinger_mean_revert`, `macd_momentum`)
3. **Build:** `make build`
4. **Start:** `make up`
5. **Migrate:** `make migrate`
6. **Verify:** `make logs` (look for "Starting Up" and strategy loading messages)
7. **Dashboard:** `http://localhost:8080`

---

## 16. Known Issues & Observations

### Architecture Issues

1. **Config inconsistency:** EMA strategy reads params from Config dataclass; Bollinger and MACD read directly from `os.getenv()`. The latter bypass centralized validation.

2. **StrategySignal field overloading:** Fields are named `ema_fast`, `ema_slow`, `rsi` but Bollinger stores band values and MACD stores MACD/signal/histogram in these fields. Downstream code interpreting field names literally will misread non-EMA strategies.

3. **Dashboard data staleness:** Dashboard prices update only at candle-close boundaries (not in real time). The exchange WebSocket `MarketState` runs continuously but is NOT wired into the dashboard's `SharedState`.

4. **MarketState thread safety:** `ws_client.py`'s `MarketState` has no locking. Concurrent reads from the main thread during WebSocket handler writes could produce torn reads (especially relevant if GIL is relaxed in future Python).

5. **Unused dependency:** `python-telegram-bot` is in requirements.txt but `telegram_alerts.py` uses raw `requests` for the Telegram Bot API.

6. **No dashboard authentication:** All endpoints including the kill switch are unprotected. Any network client can access them.

7. **Kill switch POST protection:** Only a header value (`X-Kill-Confirm: yes`), no token or credential.

### Missing Features

8. **No equity curve or max drawdown tracking:** Only daily PnL guard exists. No cumulative high-water mark or peak-to-trough drawdown.

9. **No trailing stop-loss:** SL/TP are static percentage-based levels. No trailing stop, ATR-based stop, or dynamic adjustment.

10. **Breakeven trades counted as losses:** In PnL summary, `pnl <= 0` counts as a loss, slightly deflating win rate.

11. **No backtesting framework:** Strategies can only be tested live on testnet.

### Strengths

12. **Defensive reconciliation:** Every candle tick runs full position/order reconciliation, orphan cleanup, and stale-order sync. Handles mid-crash recovery, manual interventions, and liquidations.

13. **Belt-and-braces order management:** Individual cancels + fallback `cancel_all_orders`. Stored counterpart IDs for O(1) SL/TP cancellation.

14. **Comprehensive audit trail:** Every signal, order, and trade state change persisted to DB and sent to Telegram.

15. **Fail-fast configuration:** Missing required env vars terminate the process immediately.

16. **Thread-safe SharedState:** Dashboard state protected by lock, clean separation of concerns.
