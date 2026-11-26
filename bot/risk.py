"""Risk management: position sizing, daily loss limit, kill switch.

Centralises all risk checks so the executor never opens a trade
that violates the configured risk parameters.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any

from bot.config import cfg
from bot.database import Trade, get_session

logger = logging.getLogger(__name__)


class RiskManager:
    """Enforces position-sizing rules, daily loss limits, and the kill switch."""

    def __init__(self) -> None:
        """Initialise the risk manager with fresh daily state."""
        self._daily_pnl: float = 0.0
        self._daily_reset_date: datetime = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        self._trading_halted: bool = False

    # ── Position sizing ─────────────────────────────────────

    def calculate_position_size(self, entry_price: float) -> float:
        """Calculate the position size based on fixed-fractional risk.

        Formula: (capital × risk_per_trade × leverage) / entry_price

        Args:
            entry_price: Expected entry price (mid-price).

        Returns:
            Position size in base currency (e.g. BTC).
        """
        if entry_price <= 0:
            logger.error("Invalid entry price: %.2f", entry_price)
            return 0.0

        notional = cfg.capital * cfg.risk_per_trade  # 2% of capital
        position_value = notional * cfg.leverage      # leveraged value
        qty = position_value / entry_price

        logger.info(
            "Position size: %.6f (capital=%.0f, risk=%.1f%%, leverage=%dx, price=%.2f)",
            qty,
            cfg.capital,
            cfg.risk_per_trade * 100,
            cfg.leverage,
            entry_price,
        )
        return round(qty, 6)

    def calculate_stop_loss(self, entry_price: float, side: str) -> float:
        """Calculate the stop-loss price.

        Args:
            entry_price: Trade entry price.
            side: 'buy' (long) or 'sell' (short).

        Returns:
            Stop-loss price.
        """
        if side == "buy":
            sl = entry_price * (1 - cfg.stop_loss_pct)
        else:
            sl = entry_price * (1 + cfg.stop_loss_pct)
        return round(sl, 2)

    def calculate_take_profit(self, entry_price: float, side: str) -> float:
        """Calculate the take-profit price.

        Args:
            entry_price: Trade entry price.
            side: 'buy' (long) or 'sell' (short).

        Returns:
            Take-profit price.
        """
        if side == "buy":
            tp = entry_price * (1 + cfg.take_profit_pct)
        else:
            tp = entry_price * (1 - cfg.take_profit_pct)
        return round(tp, 2)

    def estimate_fees(self, qty: float, price: float) -> float:
        """Estimate the trading fees for an order.

        Args:
            qty: Order quantity in base currency.
            price: Order price.

        Returns:
            Estimated fee in USDT.
        """
        notional = qty * price
        fee = notional * cfg.maker_fee
        return round(fee, 4)

    # ── Open-position check ─────────────────────────────────

    def count_open_trades(self) -> int:
        """Count the number of currently open or pending trades in the database.

        Includes both 'open' (filled, with active position) and 'pending'
        (entry order placed, awaiting fill) trades so the position limit
        accounts for entries that haven't filled yet.

        Returns:
            Number of trades with status 'open' or 'pending'.
        """
        session = get_session()
        try:
            count = (
                session.query(Trade)
                .filter(Trade.status.in_(["open", "pending"]))
                .count()
            )
            return count
        finally:
            session.close()

    def can_open_new_position(self) -> bool:
        """Check if a new position can be opened within risk limits.

        Returns:
            True if we're below the max open positions limit and trading isn't halted.
        """
        if self.is_kill_switch_active():
            logger.warning("Kill switch is active — cannot open new positions.")
            return False

        if self._trading_halted:
            logger.warning("Trading halted (daily loss limit) — cannot open new positions.")
            return False

        open_count = self.count_open_trades()
        if open_count >= cfg.max_open_positions:
            logger.info(
                "Max open positions reached (%d/%d) — skipping.",
                open_count,
                cfg.max_open_positions,
            )
            return False

        return True

    # ── Daily loss limit ────────────────────────────────────

    def _check_daily_reset(self) -> None:
        """Reset daily PnL tracker if we've crossed into a new UTC day."""
        now = datetime.now(timezone.utc)
        current_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if current_day > self._daily_reset_date:
            logger.info(
                "New UTC day detected — resetting daily PnL (was %.2f).",
                self._daily_pnl,
            )
            self._daily_pnl = 0.0
            self._daily_reset_date = current_day
            self._trading_halted = False

    def update_daily_pnl(self, pnl: float) -> None:
        """Add a trade's PnL to the daily running total and check limit.

        Args:
            pnl: The realised PnL of a closed trade.
        """
        self._check_daily_reset()
        self._daily_pnl += pnl
        logger.info("Daily PnL updated: %.2f (total: %.2f)", pnl, self._daily_pnl)

    def is_daily_limit_breached(self) -> bool:
        """Check if the daily loss limit has been breached.

        Returns:
            True if the daily PnL has dropped below the configured limit.
        """
        self._check_daily_reset()
        limit = -(cfg.capital * cfg.daily_loss_limit)
        if self._daily_pnl <= limit:
            self._trading_halted = True
            logger.critical(
                "🚨 DAILY LOSS LIMIT HIT: PnL=%.2f, Limit=%.2f — HALTING TRADING.",
                self._daily_pnl,
                limit,
            )
            return True
        return False

    @property
    def trading_halted(self) -> bool:
        """Whether trading is currently halted due to daily loss limit."""
        self._check_daily_reset()
        return self._trading_halted

    @property
    def daily_pnl(self) -> float:
        """Current daily PnL total."""
        self._check_daily_reset()
        return self._daily_pnl

    # ── Kill switch ─────────────────────────────────────────

    @staticmethod
    def is_kill_switch_active() -> bool:
        """Check if the kill switch is engaged via environment variable.

        Reads KILL_SWITCH from the environment on every call so it can
        be toggled at runtime (e.g. via Docker env update).

        Returns:
            True if KILL_SWITCH is set to 'true'.
        """
        return os.getenv("KILL_SWITCH", "false").lower() in ("true", "1", "yes")

    # ── Reload state on restart ─────────────────────────────

    def reload_daily_pnl_from_db(self) -> None:
        """Reload today's PnL from the database after a restart.

        Sums the PnL of all trades closed today (UTC).
        """
        session = get_session()
        try:
            today_start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            trades = (
                session.query(Trade)
                .filter(
                    Trade.status == "closed",
                    Trade.exit_time >= today_start,
                )
                .all()
            )
            self._daily_pnl = sum(t.pnl or 0.0 for t in trades)
            logger.info(
                "Reloaded daily PnL from DB: %.2f (%d trades today).",
                self._daily_pnl,
                len(trades),
            )
            if self.is_daily_limit_breached():
                self._trading_halted = True
        finally:
            session.close()
