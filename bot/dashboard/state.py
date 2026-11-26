"""Thread-safe shared state object.

The bot's main strategy loop writes live data here after each tick.
The FastAPI dashboard reads from it for the WebSocket /ws/live feed,
avoiding direct exchange API calls on every browser connection.
"""

from __future__ import annotations

import threading
import time
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SharedState:
    """Thread-safe container for live bot state shared between bot and dashboard.

    The bot writes to this after each strategy loop iteration.
    The dashboard reads from it every 2 seconds for WebSocket broadcasts.
    """

    def __init__(self) -> None:
        """Initialise shared state with default values and a threading lock."""
        self._lock = threading.Lock()
        self._bot_start_time: Optional[float] = None
        self._bot_status: str = "stopped"  # "running", "halted", "stopped"

        # Price data
        self._mid_price: float = 0.0
        self._bid_price: float = 0.0
        self._ask_price: float = 0.0
        self._change_24h: float = 0.0
        self._price_updated_at: Optional[str] = None

        # Account data
        self._balance: float = 0.0
        self._unrealized_pnl: float = 0.0
        self._daily_pnl: float = 0.0

        # Indicator data
        self._ema_fast: float = 0.0
        self._ema_slow: float = 0.0
        self._rsi: float = 0.0
        self._crossover_state: str = "Neutral"
        self._last_signal_time: Optional[str] = None
        self._last_signal_direction: Optional[str] = None
        self._last_signal_action: Optional[str] = None

        # Positions
        self._positions: List[Dict[str, Any]] = []

        # Next candle close — stored as an absolute UTC epoch timestamp
        self._next_candle_close_ts: float = 0.0

        # Candle data for chart
        self._candles: List[Dict[str, Any]] = []
        self._ema_fast_series: List[Optional[float]] = []
        self._ema_slow_series: List[Optional[float]] = []

        # Log buffer (circular buffer of last 200 lines)
        self._log_buffer: List[Dict[str, str]] = []
        self._log_buffer_max: int = 200

    # ── Bot lifecycle ───────────────────────────────────────

    def set_bot_started(self) -> None:
        """Mark the bot as started and record the start time."""
        with self._lock:
            self._bot_start_time = time.time()
            self._bot_status = "running"

    def set_bot_stopped(self) -> None:
        """Mark the bot as stopped."""
        with self._lock:
            self._bot_status = "stopped"

    def set_bot_halted(self) -> None:
        """Mark the bot as halted (daily loss limit)."""
        with self._lock:
            self._bot_status = "halted"

    def set_bot_running(self) -> None:
        """Mark the bot as running again (after halt ends)."""
        with self._lock:
            self._bot_status = "running"

    # ── Price updates ───────────────────────────────────────

    def update_price(
        self,
        mid: float,
        bid: float,
        ask: float,
        change_24h: float,
    ) -> None:
        """Update live price data.

        Args:
            mid: Current mid-price.
            bid: Best bid price.
            ask: Best ask price.
            change_24h: 24-hour price change percentage.
        """
        with self._lock:
            self._mid_price = mid
            self._bid_price = bid
            self._ask_price = ask
            self._change_24h = change_24h
            self._price_updated_at = datetime.now(timezone.utc).isoformat()

    # ── Account updates ────────────────────────────────────

    def update_account(
        self,
        balance: float,
        unrealized_pnl: float,
        daily_pnl: float,
    ) -> None:
        """Update account balance and PnL data.

        Args:
            balance: Current account balance in USDT.
            unrealized_pnl: Sum of unrealized PnL across open positions.
            daily_pnl: Today's cumulative realised PnL.
        """
        with self._lock:
            self._balance = balance
            self._unrealized_pnl = unrealized_pnl
            self._daily_pnl = daily_pnl

    # ── Indicator updates ──────────────────────────────────

    def update_indicators(
        self,
        ema_fast: float,
        ema_slow: float,
        rsi: float,
        crossover_state: str,
    ) -> None:
        """Update current indicator values.

        Args:
            ema_fast: Current fast EMA value.
            ema_slow: Current slow EMA value.
            rsi: Current RSI value.
            crossover_state: 'Bullish', 'Bearish', or 'Neutral'.
        """
        with self._lock:
            self._ema_fast = ema_fast
            self._ema_slow = ema_slow
            self._rsi = rsi
            self._crossover_state = crossover_state

    def update_last_signal(
        self, direction: str, action: str
    ) -> None:
        """Record the most recent signal evaluation.

        Args:
            direction: Signal direction ('buy' or 'sell').
            action: What happened ('triggered', 'skipped', etc.).
        """
        with self._lock:
            self._last_signal_time = datetime.now(timezone.utc).isoformat()
            self._last_signal_direction = direction
            self._last_signal_action = action

    # ── Positions ──────────────────────────────────────────

    def update_positions(self, positions: List[Dict[str, Any]]) -> None:
        """Update the list of open positions.

        Args:
            positions: List of position dicts with trade details.
        """
        with self._lock:
            self._positions = positions

    # ── Candle data ────────────────────────────────────────

    def update_candles(
        self,
        candles: List[Dict[str, Any]],
        ema_fast_series: List[Optional[float]],
        ema_slow_series: List[Optional[float]],
    ) -> None:
        """Update candle data for the price chart.

        Args:
            candles: List of OHLCV candle dicts.
            ema_fast_series: EMA-fast values aligned with candles.
            ema_slow_series: EMA-slow values aligned with candles.
        """
        with self._lock:
            self._candles = candles
            self._ema_fast_series = ema_fast_series
            self._ema_slow_series = ema_slow_series

    # ── Next candle countdown ──────────────────────────────

    def update_next_candle_seconds(self, seconds: int) -> None:
        """Store the next candle close as an absolute timestamp.

        Converts a relative "seconds from now" value into an absolute
        epoch timestamp so that ``get_live_snapshot()`` can recompute
        a fresh countdown on every call.

        Args:
            seconds: Seconds from now until the next candle closes.
        """
        with self._lock:
            self._next_candle_close_ts = time.time() + seconds

    # ── Log buffer ─────────────────────────────────────────

    def add_log_line(self, level: str, message: str) -> None:
        """Append a log line to the circular buffer.

        Args:
            level: Log level string (INFO, WARNING, ERROR, etc.).
            message: The log message text.
        """
        with self._lock:
            entry = {
                "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "level": level,
                "message": message,
            }
            self._log_buffer.append(entry)
            if len(self._log_buffer) > self._log_buffer_max:
                self._log_buffer = self._log_buffer[-self._log_buffer_max:]

    def get_recent_logs(self, count: int = 50) -> List[Dict[str, str]]:
        """Return the most recent log lines.

        Args:
            count: Number of recent lines to return.

        Returns:
            List of log entry dicts.
        """
        with self._lock:
            return list(self._log_buffer[-count:])

    # ── Snapshot for WebSocket broadcast ───────────────────

    def get_live_snapshot(self) -> Dict[str, Any]:
        """Return a complete snapshot of the live state for WebSocket broadcast.

        Returns:
            Dict matching the /ws/live payload schema.
        """
        with self._lock:
            uptime = 0.0
            if self._bot_start_time:
                uptime = time.time() - self._bot_start_time

            return {
                "price": {
                    "mid": self._mid_price,
                    "bid": self._bid_price,
                    "ask": self._ask_price,
                    "change_24h": self._change_24h,
                    "updated_at": self._price_updated_at,
                },
                "positions": self._positions,
                "account": {
                    "balance": self._balance,
                    "unrealized_pnl": self._unrealized_pnl,
                    "daily_pnl": self._daily_pnl,
                },
                "indicators": {
                    "ema9": self._ema_fast,
                    "ema21": self._ema_slow,
                    "rsi": self._rsi,
                    "crossover": self._crossover_state,
                    "last_signal_time": self._last_signal_time,
                    "last_signal_direction": self._last_signal_direction,
                    "last_signal_action": self._last_signal_action,
                },
                "bot_status": self._bot_status,
                "uptime_seconds": uptime,
                "next_candle_close_seconds": max(0, int(self._next_candle_close_ts - time.time())),
            }

    def get_candle_snapshot(self) -> Dict[str, Any]:
        """Return candle data with EMA overlays for the chart.

        Returns:
            Dict with candles and EMA series arrays.
        """
        with self._lock:
            return {
                "candles": list(self._candles),
                "ema_fast": list(self._ema_fast_series),
                "ema_slow": list(self._ema_slow_series),
            }


# ── Module-level singleton ─────────────────────────────────

shared_state = SharedState()
