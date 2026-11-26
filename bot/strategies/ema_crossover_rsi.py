"""EMA Crossover + RSI Confirmation strategy.

On every closed candle the strategy:
  1. Computes EMA-fast, EMA-slow, and RSI via pandas-ta.
  2. Detects EMA crossover direction.
  3. Applies RSI confirmation filters.
  4. Returns a StrategySignal ('buy', 'sell') or None.

Environment variables (all optional, with defaults):
  EMA_FAST_PERIOD  — Fast EMA length (default: 9)
  EMA_SLOW_PERIOD  — Slow EMA length (default: 21)
  RSI_PERIOD       — RSI period (default: 14)
  RSI_BUY_LOW      — Min RSI for buy signals (default: 40)
  RSI_BUY_HIGH     — Max RSI for buy signals (default: 65)
  RSI_SELL_LOW     — Min RSI for sell signals (default: 35)
  RSI_SELL_HIGH    — Max RSI for sell signals (default: 60)
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

import pandas as pd
import pandas_ta as ta

from bot.strategies.base_strategy import BaseStrategy, StrategySignal

logger = logging.getLogger(__name__)


class EMACrossoverRSI(BaseStrategy):
    """EMA Crossover with RSI confirmation — the original bot strategy."""

    def __init__(self, config: Any) -> None:
        self._cfg = config
        self._ema_fast_period: int = config.ema_fast_period
        self._ema_slow_period: int = config.ema_slow_period
        self._rsi_period: int = config.rsi_period
        self._rsi_buy_low: float = config.rsi_buy_low
        self._rsi_buy_high: float = config.rsi_buy_high
        self._rsi_sell_low: float = config.rsi_sell_low
        self._rsi_sell_high: float = config.rsi_sell_high

    def get_name(self) -> str:
        return "EMA Crossover + RSI Confirmation"

    def get_required_candle_count(self) -> int:
        return self._ema_slow_period + 10

    def evaluate(self, candles: List[List[Any]]) -> Optional[StrategySignal]:
        """Run the full EMA crossover + RSI pipeline on raw candle data."""
        if len(candles) < self._ema_slow_period + 5:
            logger.warning(
                "Not enough candles (%d) to compute indicators (need >= %d).",
                len(candles),
                self._ema_slow_period + 5,
            )
            return None

        df = self._compute_indicators(candles)

        crossover = self._detect_crossover(df)
        if crossover is None:
            logger.debug("No EMA crossover detected.")
            return None

        last_closed = df.iloc[-2]
        rsi_value = float(last_closed["rsi"])
        ema_fast = float(last_closed["ema_fast"])
        ema_slow = float(last_closed["ema_slow"])
        close_price = float(last_closed["close"])

        if not self._check_rsi_filter(rsi_value, crossover):
            logger.info(
                "EMA crossover (%s) detected but RSI filter rejected (RSI=%.2f).",
                crossover,
                rsi_value,
            )
            return None

        signal_type = "buy" if crossover == "bullish" else "sell"

        signal = StrategySignal(
            signal_type=signal_type,
            price=close_price,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            rsi=rsi_value,
        )

        logger.info(
            "SIGNAL: %s | Price=%.2f | EMA-fast=%.2f | EMA-slow=%.2f | RSI=%.2f",
            signal_type.upper(),
            close_price,
            ema_fast,
            ema_slow,
            rsi_value,
        )

        return signal

    # ── Internal helpers ──────────────────────────────────────

    def _compute_indicators(self, candles: List[List[Any]]) -> pd.DataFrame:
        """Convert raw OHLCV candles to a DataFrame with EMA and RSI columns."""
        df = pd.DataFrame(
            candles, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df["ema_fast"] = ta.ema(df["close"], length=self._ema_fast_period)
        df["ema_slow"] = ta.ema(df["close"], length=self._ema_slow_period)
        df["rsi"] = ta.rsi(df["close"], length=self._rsi_period)
        return df

    def _detect_crossover(self, df: pd.DataFrame) -> Optional[str]:
        """Detect whether the fast EMA just crossed above or below the slow EMA."""
        if len(df) < 3:
            return None

        prev = df.iloc[-3]
        curr = df.iloc[-2]

        prev_fast = prev["ema_fast"]
        prev_slow = prev["ema_slow"]
        curr_fast = curr["ema_fast"]
        curr_slow = curr["ema_slow"]

        if pd.isna(prev_fast) or pd.isna(prev_slow) or pd.isna(curr_fast) or pd.isna(curr_slow):
            return None

        if prev_fast <= prev_slow and curr_fast > curr_slow:
            return "bullish"
        if prev_fast >= prev_slow and curr_fast < curr_slow:
            return "bearish"

        return None

    def _check_rsi_filter(self, rsi_value: float, direction: str) -> bool:
        """Apply RSI confirmation filter for a given trade direction."""
        if pd.isna(rsi_value):
            return False

        if direction == "bullish":
            return self._rsi_buy_low <= rsi_value <= self._rsi_buy_high
        elif direction == "bearish":
            return self._rsi_sell_low <= rsi_value <= self._rsi_sell_high

        return False


# ── Module-level convenience functions (backward compatibility) ────


def compute_indicators(candles: List[List[Any]]) -> pd.DataFrame:
    """Backward-compatible wrapper used by the dashboard."""
    from bot.config import cfg

    strategy = EMACrossoverRSI(cfg)
    return strategy._compute_indicators(candles)


def detect_crossover(df: pd.DataFrame) -> Optional[str]:
    """Backward-compatible wrapper used by the dashboard."""
    from bot.config import cfg

    strategy = EMACrossoverRSI(cfg)
    return strategy._detect_crossover(df)
