"""PnL calculation and periodic summary generation.

Queries the database for closed trades within a time window,
computes aggregate statistics, stores a PnlSummary row,
and sends the results via Telegram.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from bot.config import cfg
from bot.database import PnlSummary, Trade, get_session
from bot import telegram_alerts

logger = logging.getLogger(__name__)


def calculate_trade_pnl(
    side: str,
    entry_price: float,
    exit_price: float,
    qty: float,
    leverage: int,
) -> float:
    """Calculate the realised PnL for a single trade.

    Args:
        side: 'buy' (long) or 'sell' (short).
        entry_price: Entry fill price.
        exit_price: Exit fill price.
        qty: Position size in base currency.
        leverage: Leverage used.

    Returns:
        Realised PnL in USDT (before fees).
    """
    if side == "buy":
        pnl = (exit_price - entry_price) * qty
    else:
        pnl = (entry_price - exit_price) * qty
    return round(pnl, 4)


def calculate_fees(qty: float, price: float) -> float:
    """Estimate fees for a single leg (entry or exit).

    Args:
        qty: Quantity traded.
        price: Execution price.

    Returns:
        Fee in USDT.
    """
    notional = qty * price
    return round(notional * cfg.maker_fee, 4)


def generate_pnl_summary(hours: int = 6) -> None:
    """Generate a PnL summary for the last N hours and send via Telegram.

    Queries all trades closed within the window, computes aggregate
    metrics, persists a PnlSummary row, and fires a Telegram alert.

    Args:
        hours: Number of hours to look back (default: 6).
    """
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        period_start = now - timedelta(hours=hours)

        # Fetch all closed trades in the window
        trades = (
            session.query(Trade)
            .filter(
                Trade.status == "closed",
                Trade.exit_time >= period_start,
                Trade.exit_time <= now,
            )
            .all()
        )

        total_trades = len(trades)
        if total_trades == 0:
            logger.info("No closed trades in the last %d hours — nothing to summarise.", hours)
            # Still count open positions
            open_positions = session.query(Trade).filter(Trade.status == "open").count()
            telegram_alerts.alert_pnl_summary(
                total_trades=0,
                win_rate=0.0,
                gross_pnl=0.0,
                net_pnl=0.0,
                best_trade=0.0,
                worst_trade=0.0,
                open_positions=open_positions,
                fees_paid=0.0,
            )
            return

        pnl_values = [t.pnl or 0.0 for t in trades]
        fee_values = [t.fees or 0.0 for t in trades]

        wins = sum(1 for p in pnl_values if p > 0)
        losses = sum(1 for p in pnl_values if p <= 0)
        gross_pnl = sum(pnl_values)
        fees_paid = sum(fee_values)
        net_pnl = gross_pnl - fees_paid
        win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0.0
        best_trade = max(pnl_values) if pnl_values else 0.0
        worst_trade = min(pnl_values) if pnl_values else 0.0
        open_positions = session.query(Trade).filter(Trade.status == "open").count()

        # Persist summary
        summary = PnlSummary(
            period_start=period_start,
            period_end=now,
            total_trades=total_trades,
            wins=wins,
            losses=losses,
            gross_pnl=round(gross_pnl, 4),
            net_pnl=round(net_pnl, 4),
            fees_paid=round(fees_paid, 4),
        )
        session.add(summary)
        session.commit()

        logger.info(
            "PnL Summary (%dh): trades=%d, wins=%d, losses=%d, net=%.2f USDT",
            hours,
            total_trades,
            wins,
            losses,
            net_pnl,
        )

        # Send Telegram alert
        telegram_alerts.alert_pnl_summary(
            total_trades=total_trades,
            win_rate=win_rate,
            gross_pnl=round(gross_pnl, 2),
            net_pnl=round(net_pnl, 2),
            best_trade=round(best_trade, 2),
            worst_trade=round(worst_trade, 2),
            open_positions=open_positions,
            fees_paid=round(fees_paid, 2),
        )

    except Exception as exc:
        logger.error("Error generating PnL summary: %s", exc)
        session.rollback()
        telegram_alerts.alert_error(f"PnL summary generation failed: {exc}")
    finally:
        session.close()
