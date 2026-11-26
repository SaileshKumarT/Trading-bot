"""Entry point for the Delta Exchange trading bot.

Responsibilities:
  - Configure logging (file + console, daily rotation).
  - Initialise database, exchange, risk manager, and executor.
  - Set leverage on the exchange.
  - Reload state from DB (open positions and pending orders).
  - Schedule the strategy loop to run at each 15m candle close.
  - Schedule 6-hour PnL summary reports.
  - Handle SIGINT/SIGTERM for graceful shutdown.
  - React to the kill switch at every loop iteration.
  - Launch the live dashboard (FastAPI) in a background thread.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from typing import Optional

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.config import cfg
from bot.database import init_db, create_tables, get_session, Trade
from bot.exchange import Exchange
from bot.executor import Executor
from bot.pnl import generate_pnl_summary
from bot.risk import RiskManager
from bot.strategies import get_strategy
from bot.strategies.base_strategy import BaseStrategy
from bot.strategies.ema_crossover_rsi import compute_indicators, detect_crossover
from bot import telegram_alerts
from bot.dashboard.state import shared_state
from bot.dashboard.server import start_dashboard_in_thread, DashboardLogHandler

# ── Logging setup ───────────────────────────────────────────

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")


def setup_logging() -> None:
    """Configure root logger with console and rotating file handlers.

    - Console: always active, level from config.
    - File: daily rotation, 30-day retention, written to logs/ directory.
    """
    os.makedirs(LOG_DIR, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, cfg.log_level, logging.INFO))

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Rotating file handler (daily, keep 30 days)
    log_path = os.path.join(LOG_DIR, "bot.log")
    file_handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=30,
        utc=True,
    )
    file_handler.setFormatter(formatter)
    file_handler.suffix = "%Y-%m-%d"
    root_logger.addHandler(file_handler)

    # Dashboard log handler (feeds logs into shared state for WebSocket)
    dashboard_handler = DashboardLogHandler()
    dashboard_handler.setFormatter(formatter)
    root_logger.addHandler(dashboard_handler)

    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# ── Global state ────────────────────────────────────────────

logger: Optional[logging.Logger] = None
scheduler: Optional[BlockingScheduler] = None
executor_instance: Optional[Executor] = None
exchange_instance: Optional[Exchange] = None
risk_manager: Optional[RiskManager] = None
active_strategy: Optional[BaseStrategy] = None


# ── Orphaned position sync ─────────────────────────────────


def _sync_orphaned_positions() -> None:
    """Check for DB trades marked 'open' that have no matching exchange position.

    This handles the case where the bot was killed without a clean shutdown
    and positions were closed externally or by the exchange.  Orphaned DB
    trades are marked as closed with exit_reason='orphan_sync'.
    """
    assert exchange_instance is not None
    assert logger is not None

    session = get_session()
    try:
        open_trades = session.query(Trade).filter(Trade.status == "open").all()
        if not open_trades:
            return

        # Fetch real positions from the exchange
        exchange_positions = exchange_instance.fetch_positions([cfg.trading_symbol])
        exchange_has_position = any(
            float(p.get("contracts", 0) or 0) > 0 for p in exchange_positions
        )

        if exchange_has_position:
            logger.info("Exchange has open positions — DB trades are consistent.")
            return

        # No exchange positions but DB has open trades → orphaned
        for trade in open_trades:
            logger.warning(
                "Orphaned trade #%d (%s %s) — no matching exchange position. Marking closed.",
                trade.id, trade.side, trade.symbol,
            )
            trade.status = "closed"
            trade.exit_time = datetime.now(timezone.utc)
            trade.exit_reason = "orphan_sync"

        session.commit()
        logger.info("Synced %d orphaned trades.", len(open_trades))
    except Exception as exc:
        logger.error("Error during orphaned position sync: %s", exc)
        session.rollback()
    finally:
        session.close()


# ── Strategy loop ───────────────────────────────────────────


def _parse_timeframe_minutes(tf: str) -> int:
    """Parse a timeframe string like '1m', '15m', '1h' into minutes."""
    tf = tf.strip().lower()
    if tf.endswith("h"):
        return int(tf[:-1]) * 60
    if tf.endswith("d"):
        return int(tf[:-1]) * 1440
    # Default: ends with 'm' or bare number
    return int(tf.rstrip("m"))


def _calculate_next_candle_seconds() -> int:
    """Calculate seconds until the next candle close for the configured timeframe.

    Returns:
        Number of seconds until the next candle boundary.
    """
    interval_min = _parse_timeframe_minutes(cfg.timeframe)
    now = datetime.now(timezone.utc)

    if interval_min < 1:
        interval_min = 1

    if interval_min < 60:
        # Sub-hour: align to minute boundaries within the hour
        minute = now.minute
        next_boundary = ((minute // interval_min) + 1) * interval_min
        seconds_left = (next_boundary - minute) * 60 - now.second
    else:
        # Hourly+: align to hour boundaries
        interval_hours = interval_min // 60
        hour = now.hour
        next_boundary = ((hour // interval_hours) + 1) * interval_hours
        seconds_left = (
            (next_boundary - hour) * 3600
            - now.minute * 60
            - now.second
        )

    interval_secs = interval_min * 60
    if seconds_left <= 0:
        seconds_left += interval_secs
    return seconds_left


def _update_dashboard_state(candles: list) -> None:
    """Push live data into shared state for the dashboard.

    Args:
        candles: Raw OHLCV candle list from the exchange.
    """
    try:
        assert exchange_instance is not None
        assert risk_manager is not None

        # Price data
        book = exchange_instance.fetch_order_book(cfg.trading_symbol)
        bid = book["bids"][0][0] if book.get("bids") else 0
        ask = book["asks"][0][0] if book.get("asks") else 0
        mid = (bid + ask) / 2

        change_24h = 0.0
        try:
            ticker = exchange_instance.fetch_ticker(cfg.trading_symbol)
            change_24h = ticker.get("percentage", 0) or 0
        except Exception:
            pass

        shared_state.update_price(mid, bid, ask, change_24h)

        # Account data
        balance = cfg.capital  # Testnet simulated balance
        try:
            bal_resp = exchange_instance.fetch_balance()
            usdt_bal = bal_resp.get("USDT", {}).get("total", cfg.capital)
            if usdt_bal:
                balance = float(usdt_bal)
        except Exception:
            pass

        # Unrealized PnL from open positions
        session = get_session()
        try:
            open_trades = session.query(Trade).filter(Trade.status == "open").all()
            unrealized = 0.0
            positions_data = []
            for t in open_trades:
                entry_px = t.entry_price or 0
                if t.side == "buy":
                    upnl = (mid - entry_px) * t.qty if mid > 0 and entry_px > 0 else 0
                else:
                    upnl = (entry_px - mid) * t.qty if mid > 0 and entry_px > 0 else 0
                unrealized += upnl
                positions_data.append({
                    "id": t.id, "symbol": t.symbol, "side": t.side,
                    "entry_price": entry_px, "qty": t.qty,
                    "unrealized_pnl": round(upnl, 2),
                })
        finally:
            session.close()

        shared_state.update_account(balance, unrealized, risk_manager.daily_pnl)
        shared_state.update_positions(positions_data)

        # Indicators from candle data
        if candles and len(candles) > 25:
            import pandas as pd
            df = compute_indicators(candles)
            last = df.iloc[-2]  # Last closed candle
            ema_f = last["ema_fast"] if not pd.isna(last["ema_fast"]) else 0
            ema_s = last["ema_slow"] if not pd.isna(last["ema_slow"]) else 0
            rsi_v = last["rsi"] if not pd.isna(last["rsi"]) else 0

            cross = detect_crossover(df)
            cross_state = "Neutral"
            if cross == "bullish":
                cross_state = "Bullish"
            elif cross == "bearish":
                cross_state = "Bearish"
            elif ema_f > ema_s:
                cross_state = "Bullish"
            elif ema_f < ema_s:
                cross_state = "Bearish"

            shared_state.update_indicators(ema_f, ema_s, rsi_v, cross_state)

            # Candle data for chart
            candle_dicts = []
            ema_fast_list = []
            ema_slow_list = []
            for i, row in df.iterrows():
                candle_dicts.append({
                    "timestamp": row["timestamp"].isoformat() if hasattr(row["timestamp"], "isoformat") else str(row["timestamp"]),
                    "open": row["open"], "high": row["high"],
                    "low": row["low"], "close": row["close"],
                    "volume": row["volume"],
                })
                ema_fast_list.append(None if pd.isna(row["ema_fast"]) else round(row["ema_fast"], 2))
                ema_slow_list.append(None if pd.isna(row["ema_slow"]) else round(row["ema_slow"], 2))

            shared_state.update_candles(candle_dicts, ema_fast_list, ema_slow_list)

        # Countdown
        shared_state.update_next_candle_seconds(_calculate_next_candle_seconds())

        # Bot status
        if risk_manager.trading_halted:
            shared_state.set_bot_halted()
        else:
            shared_state.set_bot_running()

    except Exception as exc:
        logging.getLogger(__name__).error("Error updating dashboard state: %s", exc)


def strategy_loop() -> None:
    """Main strategy loop — executed at every candle close.

    1. Check kill switch.
    2. Check and manage pending entry orders.
    3. Check SL/TP fills on open trades.
    4. Clean up orphaned / stale orders.
    5. Fetch fresh candles and generate signals.
    6. Execute new signals if risk allows.
    7. Update dashboard shared state.
    """
    assert logger is not None
    assert executor_instance is not None
    assert exchange_instance is not None
    assert risk_manager is not None
    assert active_strategy is not None

    logger.info("━━━ Strategy loop triggered at %s ━━━", datetime.now(timezone.utc).isoformat())

    # 1. Kill switch check (re-read env on every tick)
    if risk_manager.is_kill_switch_active():
        logger.critical("Kill switch is active — executing emergency shutdown.")
        shared_state.set_bot_stopped()
        executor_instance.execute_kill_switch()
        if scheduler:
            scheduler.shutdown(wait=False)
        return

    # 2. Daily loss limit check
    if risk_manager.trading_halted:
        logger.warning("Trading halted (daily loss limit). Checking exits only.")
        shared_state.set_bot_halted()
        executor_instance.check_open_trades()
        return

    candles = None
    try:
        # 3. Reconcile positions and orders (cancel orphaned, fix missing SL/TP)
        reconciliation_clean = True
        try:
            reconciliation_clean = executor_instance.reconcile_positions_and_orders()
        except Exception as exc:
            logger.warning("Reconciliation step failed (non-fatal): %s", exc)

        # 4. Check pending entry orders (fills or expiry)
        executor_instance.check_pending_entries()

        # 5. Check SL/TP fills on open trades
        executor_instance.check_open_trades()

        # 5b. Periodic cleanup: cancel orphaned orders and sync stale DB records
        try:
            executor_instance.cleanup_orphaned_orders()
            executor_instance.cleanup_stale_db_orders()
        except Exception as exc:
            logger.warning("Order cleanup step failed (non-fatal): %s", exc)

        # 6. Fetch candles and generate signal
        candle_limit = max(100, active_strategy.get_required_candle_count() * 2)
        candles = exchange_instance.fetch_ohlcv(
            symbol=cfg.trading_symbol,
            timeframe=cfg.timeframe,
            limit=candle_limit,
        )

        if not candles:
            logger.warning("No candle data received — skipping this tick.")
            return

        # Skip signal evaluation if reconciliation fixed inconsistencies
        if not reconciliation_clean:
            logger.info(
                "Reconciliation fixed order inconsistencies — "
                "skipping signal evaluation this iteration."
            )
        else:
            signal = active_strategy.evaluate(candles)

            if signal is not None:
                shared_state.update_last_signal(signal.signal_type, "triggered")
                executor_instance.process_signal(signal)
            else:
                logger.info("No actionable signal this candle.")

    except Exception as exc:
        logger.error("Unhandled error in strategy loop: %s", exc, exc_info=True)
        telegram_alerts.alert_error(f"Strategy loop error: {exc}")
    finally:
        # 6. Always update dashboard state
        _update_dashboard_state(candles or [])


# ── Graceful shutdown ───────────────────────────────────────


def graceful_shutdown(signum: int, frame: object) -> None:
    """Handle SIGINT/SIGTERM — cancel orders, close positions, and shut down.

    Args:
        signum: Signal number received.
        frame: Current stack frame (unused).
    """
    sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
    if logger:
        logger.info("Received %s — initiating graceful shutdown…", sig_name)

    shared_state.set_bot_stopped()

    if executor_instance and exchange_instance:
        # 1. Cancel all open orders on the exchange
        try:
            exchange_instance.cancel_all_orders(cfg.trading_symbol)
            if logger:
                logger.info("All open orders cancelled on exchange.")
        except Exception as exc:
            if logger:
                logger.error("Error cancelling orders during shutdown: %s", exc)

        # 2. Close all open positions on the exchange at market
        try:
            positions = exchange_instance.fetch_positions([cfg.trading_symbol])
            for pos in positions:
                contracts = float(pos.get("contracts", 0) or 0)
                side = pos.get("side", "")
                if contracts > 0 and side:
                    pos_side = "buy" if side == "long" else "sell"
                    exchange_instance.close_position(cfg.trading_symbol, pos_side, contracts)
                    if logger:
                        logger.info("Closed position on shutdown: %s %.6f contracts", side, contracts)
        except Exception as exc:
            if logger:
                logger.error("Error closing positions during shutdown: %s", exc)

        # 3. Cancel unfilled entry orders in DB
        try:
            executor_instance.cancel_unfilled_entries()
        except Exception as exc:
            if logger:
                logger.error("Error cancelling DB entries during shutdown: %s", exc)

        # 4. Update open trades in DB with exit_reason="bot_shutdown"
        try:
            session = get_session()
            open_trades = session.query(Trade).filter(Trade.status == "open").all()
            for trade in open_trades:
                trade.status = "closed"
                trade.exit_time = datetime.now(timezone.utc)
                trade.exit_reason = "bot_shutdown"
                if logger:
                    logger.info("Marked trade #%d as closed (bot_shutdown).", trade.id)
            session.commit()
            session.close()
        except Exception as exc:
            if logger:
                logger.error("Error updating trade records during shutdown: %s", exc)

    # Send Telegram notification
    try:
        telegram_alerts.alert_bot_stopped()
    except Exception:
        pass

    # Stop scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)

    if logger:
        logger.info("Shutdown complete.")

    sys.exit(0)


# ── Main entry point ───────────────────────────────────────


def main() -> None:
    """Initialise all components and start the scheduler."""
    global logger, scheduler, executor_instance, exchange_instance, risk_manager, active_strategy

    # 1. Logging
    setup_logging()
    logger = logging.getLogger("bot.main")
    logger.info("=" * 60)
    logger.info("  Delta Exchange Trading Bot — Starting Up")
    logger.info("=" * 60)

    # 2. Strategy selection
    active_strategy = get_strategy(cfg.active_strategy, cfg)
    logger.info("Starting bot with strategy: %s", active_strategy.get_name())

    # 3. Database
    logger.info("Initialising database…")
    init_db(cfg.database_url)
    create_tables()

    # 4. Exchange
    logger.info("Connecting to Delta Exchange India testnet…")
    exchange_instance = Exchange()
    try:
        exchange_instance.load_markets()
    except Exception as exc:
        logger.error("Failed to load markets: %s", exc)
        telegram_alerts.alert_error(f"Failed to load markets on startup: {exc}")
        # Continue anyway — markets may load on first strategy tick

    # Set leverage
    try:
        exchange_instance.set_leverage(cfg.leverage, cfg.trading_symbol)
    except Exception as exc:
        logger.warning("Could not set leverage on startup: %s", exc)

    # 5. Risk manager
    risk_manager = RiskManager()
    risk_manager.reload_daily_pnl_from_db()

    # 6. Executor
    executor_instance = Executor(exchange_instance, risk_manager)
    executor_instance.reload_state()

    # 6b. Sync orphaned positions — close DB trades that no longer exist on exchange
    try:
        _sync_orphaned_positions()
    except Exception as exc:
        logger.warning("Orphaned position sync failed (non-fatal): %s", exc)

    # 7. Kill switch check before starting
    if risk_manager.is_kill_switch_active():
        logger.critical("Kill switch is active on startup — executing and exiting.")
        executor_instance.execute_kill_switch()
        return

    # 8. Register signal handlers
    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)

    # 9. Dashboard server (background thread)
    dashboard_enabled = os.getenv("DASHBOARD_ENABLED", "true").lower() in ("true", "1", "yes")
    if dashboard_enabled:
        dashboard_port = int(os.getenv("DASHBOARD_PORT", "8080"))
        logger.info("Launching dashboard on port %d…", dashboard_port)
        start_dashboard_in_thread(port=dashboard_port)
    else:
        logger.info("Dashboard disabled (DASHBOARD_ENABLED=false).")

    # 10. Telegram: bot started
    telegram_alerts.alert_bot_started()
    shared_state.set_bot_started()

    # 11. Scheduler
    scheduler = BlockingScheduler(timezone="UTC")

    # Strategy loop: dynamically scheduled from cfg.timeframe
    interval_min = _parse_timeframe_minutes(cfg.timeframe)
    if interval_min < 60:
        # Sub-hour: fire at every Nth minute boundary + 5s buffer
        if interval_min == 1:
            minute_marks = "*"
        else:
            minute_marks = ",".join(str(m) for m in range(0, 60, interval_min))
        trigger = CronTrigger(minute=minute_marks, second="5", timezone="UTC")
    else:
        # Hourly+: fire at every Nth hour boundary + 5s buffer
        interval_hours = interval_min // 60
        hour_marks = ",".join(str(h) for h in range(0, 24, interval_hours))
        trigger = CronTrigger(hour=hour_marks, minute="0", second="5", timezone="UTC")

    scheduler.add_job(
        strategy_loop,
        trigger,
        id="strategy_loop",
        name=f"{active_strategy.get_name()} Strategy Loop",
        max_instances=1,
        misfire_grace_time=60,
    )

    # PnL summary: every 6 hours
    scheduler.add_job(
        generate_pnl_summary,
        CronTrigger(hour="0,6,12,18", minute="1", timezone="UTC"),
        id="pnl_summary",
        name="6-Hour PnL Summary",
        max_instances=1,
        misfire_grace_time=300,
    )

    logger.info("Scheduler started — running every %s candle close…", cfg.timeframe)
    logger.info(
        "Config: symbol=%s, timeframe=%s, leverage=%dx, capital=%.0f USDT",
        cfg.trading_symbol,
        cfg.timeframe,
        cfg.leverage,
        cfg.capital,
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        graceful_shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()
