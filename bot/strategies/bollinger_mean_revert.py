"""Bollinger Band Mean Reversion strategy.

Trades mean-reversion entries when price touches the outer Bollinger Bands,
confirmed by RSI extremes.

Buy signal:  price closes below the lower band AND RSI < oversold threshold.
Sell signal: price closes above the upper band AND RSI > overbought threshold.

Environment variables (all optional, with defaults):
  BB_PERIOD          — Bollinger Band lookback period (default: 20)
  BB_STD_DEV         — Number of standard deviations (default: 2.0)
  BB_RSI_PERIOD      — RSI period for confirmation (default: 14)
  BB_RSI_OVERSOLD    — RSI oversold threshold for buy (default: 30)
  BB_RSI_OVERBOUGHT  — RSI overbought threshold for sell (default: 70)
"""

from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

import pandas as pd
import pandas_ta as ta

from bot.strategies.base_strategy import BaseStrategy, StrategySignal

logger = logging.getLogger(__name__)


def _get_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class BollingerMeanRevert(BaseStrategy):
    """Bollinger Band mean-reversion strategy."""

    def __init__(self, config: Any) -> None:
        self._cfg = config
        self._bb_period: int = _get_int("BB_PERIOD", 20)
        self._bb_std_dev: float = _get_float("BB_STD_DEV", 2.0)
        self._rsi_period: int = _get_int("BB_RSI_PERIOD", 14)
        self._rsi_oversold: float = _get_float("BB_RSI_OVERSOLD", 30.0)
        self._rsi_overbought: float = _get_float("BB_RSI_OVERBOUGHT", 70.0)

    def get_name(self) -> str:
        return "Bollinger Band Mean Reversion"

    def get_required_candle_count(self) -> int:
        return self._bb_period + 10

    def evaluate(self, candles: List[List[Any]]) -> Optional[StrategySignal]:
        """Evaluate Bollinger Band mean-reversion signals."""
        min_candles = self._bb_period + 5
        if len(candles) < min_candles:
            logger.warning(
                "Not enough candles (%d) for Bollinger strategy (need >= %d).",
                len(candles),
                min_candles,
            )
            return None

        df = self._compute_indicators(candles)

        last_closed = df.iloc[-2]
        close = float(last_closed["close"])
        bb_lower = last_closed["bb_lower"]
        bb_upper = last_closed["bb_upper"]
        rsi_value = last_closed["rsi"]

        if pd.isna(bb_lower) or pd.isna(bb_upper) or pd.isna(rsi_value):
            logger.debug("Bollinger or RSI values not yet available (NaN).")
            return None

        bb_lower = float(bb_lower)
        bb_upper = float(bb_upper)
        rsi_value = float(rsi_value)
        bb_mid = float(last_closed["bb_mid"])

        # Buy: price below lower band + RSI oversold
        if close <= bb_lower and rsi_value < self._rsi_oversold:
            signal = StrategySignal(
                signal_type="buy",
                price=close,
                ema_fast=bb_lower,
                ema_slow=bb_mid,
                rsi=rsi_value,
            )
            logger.info(
                "SIGNAL: BUY | Price=%.2f <= BB-lower=%.2f | RSI=%.2f < %.0f",
                close, bb_lower, rsi_value, self._rsi_oversold,
            )
            return signal

        # Sell: price above upper band + RSI overbought
        if close >= bb_upper and rsi_value > self._rsi_overbought:
            signal = StrategySignal(
                signal_type="sell",
                price=close,
                ema_fast=bb_upper,
                ema_slow=bb_mid,
                rsi=rsi_value,
            )
            logger.info(
                "SIGNAL: SELL | Price=%.2f >= BB-upper=%.2f | RSI=%.2f > %.0f",
                close, bb_upper, rsi_value, self._rsi_overbought,
            )
            return signal

        logger.debug(
            "No Bollinger signal: Price=%.2f, BB=[%.2f, %.2f], RSI=%.2f",
            close, bb_lower, bb_upper, rsi_value,
        )
        return None

    def _compute_indicators(self, candles: List[List[Any]]) -> pd.DataFrame:
        """Compute Bollinger Bands and RSI."""
        df = pd.DataFrame(
            candles, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)

        bbands = ta.bbands(df["close"], length=self._bb_period, std=self._bb_std_dev)
        if bbands is not None:
            df["bb_lower"] = bbands.iloc[:, 0]
            df["bb_mid"] = bbands.iloc[:, 1]
            df["bb_upper"] = bbands.iloc[:, 2]
        else:
            df["bb_lower"] = float("nan")
            df["bb_mid"] = float("nan")
            df["bb_upper"] = float("nan")

        df["rsi"] = ta.rsi(df["close"], length=self._rsi_period)
        return df
