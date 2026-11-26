"""MACD Momentum strategy.

Trades momentum entries based on MACD line / signal line crossovers,
optionally confirmed by the MACD histogram direction.

Buy signal:  MACD line crosses above signal line AND histogram is positive.
Sell signal: MACD line crosses below signal line AND histogram is negative.

Environment variables (all optional, with defaults):
  MACD_FAST    — Fast EMA period for MACD (default: 12)
  MACD_SLOW    — Slow EMA period for MACD (default: 26)
  MACD_SIGNAL  — Signal line period (default: 9)
"""

from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

import pandas as pd
import pandas_ta as ta

from bot.strategies.base_strategy import BaseStrategy, StrategySignal

logger = logging.getLogger(__name__)


def _get_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class MACDMomentum(BaseStrategy):
    """MACD momentum crossover strategy."""

    def __init__(self, config: Any) -> None:
        self._cfg = config
        self._fast: int = _get_int("MACD_FAST", 12)
        self._slow: int = _get_int("MACD_SLOW", 26)
        self._signal: int = _get_int("MACD_SIGNAL", 9)

    def get_name(self) -> str:
        return "MACD Momentum"

    def get_required_candle_count(self) -> int:
        return self._slow + self._signal + 10

    def evaluate(self, candles: List[List[Any]]) -> Optional[StrategySignal]:
        """Evaluate MACD crossover signals."""
        min_candles = self._slow + self._signal + 5
        if len(candles) < min_candles:
            logger.warning(
                "Not enough candles (%d) for MACD strategy (need >= %d).",
                len(candles),
                min_candles,
            )
            return None

        df = self._compute_indicators(candles)

        if len(df) < 3:
            return None

        prev = df.iloc[-3]
        curr = df.iloc[-2]

        prev_macd = prev["macd"]
        prev_signal = prev["macd_signal"]
        curr_macd = curr["macd"]
        curr_signal = curr["macd_signal"]
        curr_hist = curr["macd_hist"]

        if any(pd.isna(v) for v in [prev_macd, prev_signal, curr_macd, curr_signal, curr_hist]):
            logger.debug("MACD values not yet available (NaN).")
            return None

        prev_macd = float(prev_macd)
        prev_signal = float(prev_signal)
        curr_macd = float(curr_macd)
        curr_signal = float(curr_signal)
        curr_hist = float(curr_hist)
        close_price = float(curr["close"])

        # Bullish crossover: MACD crosses above signal + positive histogram
        if prev_macd <= prev_signal and curr_macd > curr_signal and curr_hist > 0:
            signal = StrategySignal(
                signal_type="buy",
                price=close_price,
                ema_fast=curr_macd,
                ema_slow=curr_signal,
                rsi=curr_hist,
            )
            logger.info(
                "SIGNAL: BUY | Price=%.2f | MACD=%.4f > Signal=%.4f | Hist=%.4f",
                close_price, curr_macd, curr_signal, curr_hist,
            )
            return signal

        # Bearish crossover: MACD crosses below signal + negative histogram
        if prev_macd >= prev_signal and curr_macd < curr_signal and curr_hist < 0:
            signal = StrategySignal(
                signal_type="sell",
                price=close_price,
                ema_fast=curr_macd,
                ema_slow=curr_signal,
                rsi=curr_hist,
            )
            logger.info(
                "SIGNAL: SELL | Price=%.2f | MACD=%.4f < Signal=%.4f | Hist=%.4f",
                close_price, curr_macd, curr_signal, curr_hist,
            )
            return signal

        logger.debug(
            "No MACD signal: MACD=%.4f, Signal=%.4f, Hist=%.4f",
            curr_macd, curr_signal, curr_hist,
        )
        return None

    def _compute_indicators(self, candles: List[List[Any]]) -> pd.DataFrame:
        """Compute MACD line, signal line, and histogram."""
        df = pd.DataFrame(
            candles, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)

        macd_result = ta.macd(
            df["close"], fast=self._fast, slow=self._slow, signal=self._signal
        )
        if macd_result is not None:
            df["macd"] = macd_result.iloc[:, 0]
            df["macd_hist"] = macd_result.iloc[:, 1]
            df["macd_signal"] = macd_result.iloc[:, 2]
        else:
            df["macd"] = float("nan")
            df["macd_hist"] = float("nan")
            df["macd_signal"] = float("nan")

        return df
