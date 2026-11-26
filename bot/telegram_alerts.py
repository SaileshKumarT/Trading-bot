"""Telegram alert functions.

Sends formatted messages for every bot event: signals, orders,
fills, cancellations, errors, PnL summaries, etc.
Uses python-telegram-bot in a synchronous fashion (httpx under the hood).
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

from bot.config import cfg

logger = logging.getLogger(__name__)

# ── Base send helper ────────────────────────────────────────


def _send_message(text: str) -> None:
    """Send a Telegram message via the Bot API (synchronous HTTP POST).

    Args:
        text: Message text (supports Markdown parse mode).
    """
    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": cfg.telegram_chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            logger.warning("Telegram API error %d: %s", resp.status_code, resp.text)
        else:
            logger.debug("Telegram message sent successfully.")
    except Exception as exc:
        logger.error("Failed to send Telegram message: %s", exc)


# ── Alert functions ─────────────────────────────────────────


def alert_bot_started() -> None:
    """Send an alert that the bot has started."""
    _send_message(
        "🟢 *Bot Started*\n"
        f"Symbol: `{cfg.trading_symbol}`\n"
        f"Timeframe: `{cfg.timeframe}`\n"
        f"Leverage: `{cfg.leverage}x`\n"
        f"Capital: `{cfg.capital} USDT`"
    )


def alert_bot_stopped() -> None:
    """Send an alert that the bot has stopped."""
    _send_message("🔴 *Bot Stopped*\nThe trading bot has been shut down gracefully.")


def alert_signal_detected(
    symbol: str,
    direction: str,
    price: float,
    ema_fast: float,
    ema_slow: float,
    rsi: float,
) -> None:
    """Send an alert for a new trading signal.

    Args:
        symbol: Trading pair.
        direction: 'buy' or 'sell'.
        price: Close price at signal.
        ema_fast: Fast EMA value.
        ema_slow: Slow EMA value.
        rsi: RSI value.
    """
    emoji = "🟢" if direction == "buy" else "🔴"
    _send_message(
        f"{emoji} *New Signal: {direction.upper()}*\n"
        f"Symbol: `{symbol}`\n"
        f"Price: `{price:.2f}`\n"
        f"EMA-9: `{ema_fast:.2f}` | EMA-21: `{ema_slow:.2f}`\n"
        f"RSI-14: `{rsi:.2f}`"
    )


def alert_order_placed(
    order_type: str, side: str, qty: float, price: float
) -> None:
    """Send an alert when an order is placed.

    Args:
        order_type: 'limit', 'stop_limit', etc.
        side: 'buy' or 'sell'.
        qty: Order quantity.
        price: Order price.
    """
    _send_message(
        f"📝 *Order Placed*\n"
        f"Type: `{order_type}`\n"
        f"Side: `{side.upper()}`\n"
        f"Qty: `{qty:.6f}`\n"
        f"Price: `{price:.2f}`"
    )


def alert_order_filled(side: str, qty: float, price: float) -> None:
    """Send an alert when an order is filled (entry confirmed).

    Args:
        side: 'buy' or 'sell'.
        qty: Filled quantity.
        price: Fill price.
    """
    _send_message(
        f"✅ *Order Filled — Entry Confirmed*\n"
        f"Side: `{side.upper()}`\n"
        f"Qty: `{qty:.6f}`\n"
        f"Price: `{price:.2f}`"
    )


def alert_order_cancelled(order_id: str, reason: str) -> None:
    """Send an alert when an order is cancelled.

    Args:
        order_id: Exchange order ID.
        reason: Reason for cancellation.
    """
    _send_message(
        f"❌ *Order Cancelled*\n"
        f"Order ID: `{order_id}`\n"
        f"Reason: _{reason}_"
    )


def alert_stop_loss_triggered(symbol: str, price: float, pnl: float) -> None:
    """Send an alert when a stop-loss is triggered.

    Args:
        symbol: Trading pair.
        price: Exit price.
        pnl: Realised PnL.
    """
    _send_message(
        f"🛑 *Stop-Loss Triggered*\n"
        f"Symbol: `{symbol}`\n"
        f"Exit Price: `{price:.2f}`\n"
        f"PnL: `{pnl:.2f} USDT`"
    )


def alert_take_profit_triggered(symbol: str, price: float, pnl: float) -> None:
    """Send an alert when a take-profit is triggered.

    Args:
        symbol: Trading pair.
        price: Exit price.
        pnl: Realised PnL.
    """
    _send_message(
        f"🎯 *Take-Profit Triggered*\n"
        f"Symbol: `{symbol}`\n"
        f"Exit Price: `{price:.2f}`\n"
        f"PnL: `{pnl:.2f} USDT`"
    )


def alert_daily_loss_limit(daily_pnl: float, limit: float) -> None:
    """Send an alert when the daily loss limit is hit.

    Args:
        daily_pnl: Today's cumulative PnL.
        limit: The configured loss limit value (negative).
    """
    _send_message(
        f"🚨 *DAILY LOSS LIMIT HIT — TRADING HALTED*\n"
        f"Daily PnL: `{daily_pnl:.2f} USDT`\n"
        f"Limit: `{limit:.2f} USDT`\n"
        f"Trading will resume at next UTC midnight."
    )


def alert_kill_switch() -> None:
    """Send an alert when the kill switch is activated."""
    _send_message(
        "⛔ *KILL SWITCH ACTIVATED*\n"
        "All open orders cancelled.\n"
        "All positions closed.\n"
        "Bot is shutting down."
    )


def alert_error(error_msg: str) -> None:
    """Send an alert for API errors or critical exceptions.

    Args:
        error_msg: Description of the error.
    """
    # Truncate very long error messages
    if len(error_msg) > 500:
        error_msg = error_msg[:500] + "…"
    _send_message(f"⚠️ *Error / Exception*\n```\n{error_msg}\n```")


def alert_pnl_summary(
    total_trades: int,
    win_rate: float,
    gross_pnl: float,
    net_pnl: float,
    best_trade: float,
    worst_trade: float,
    open_positions: int,
    fees_paid: float,
) -> None:
    """Send a periodic PnL summary (every 6 hours).

    Args:
        total_trades: Total number of closed trades in the period.
        win_rate: Win rate as a percentage.
        gross_pnl: Total PnL before fees.
        net_pnl: Total PnL after fees.
        best_trade: PnL of the best trade.
        worst_trade: PnL of the worst trade.
        open_positions: Number of currently open positions.
        fees_paid: Total fees paid.
    """
    _send_message(
        f"📊 *6-Hour PnL Summary*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Total Trades: `{total_trades}`\n"
        f"Win Rate: `{win_rate:.1f}%`\n"
        f"Gross PnL: `{gross_pnl:.2f} USDT`\n"
        f"Net PnL: `{net_pnl:.2f} USDT`\n"
        f"Fees Paid: `{fees_paid:.2f} USDT`\n"
        f"Best Trade: `{best_trade:.2f} USDT`\n"
        f"Worst Trade: `{worst_trade:.2f} USDT`\n"
        f"Open Positions: `{open_positions}`"
    )
