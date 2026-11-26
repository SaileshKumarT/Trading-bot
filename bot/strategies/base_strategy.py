"""Abstract base class that all trading strategies must implement."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List

import pandas as pd


@dataclass
class StrategySignal:
    """Encapsulates a trading signal with its indicator values.

    This is the universal signal format returned by all strategies.
    The executor and other downstream modules depend on this structure.
    """

    signal_type: str   # "buy" or "sell"
    price: float       # close price of the signal candle
    ema_fast: float    # latest fast EMA value (0.0 if not applicable)
    ema_slow: float    # latest slow EMA value (0.0 if not applicable)
    rsi: float         # latest RSI value (0.0 if not applicable)


class BaseStrategy(ABC):
    """Abstract base class that all trading strategies must implement.

    Subclasses receive the full config object on init and must implement
    ``evaluate``, ``get_required_candle_count``, and ``get_name``.
    """

    @abstractmethod
    def __init__(self, config: Any) -> None:
        """Initialise the strategy with the application config.

        Args:
            config: The application ``Config`` dataclass instance.
        """

    @abstractmethod
    def evaluate(self, candles: List[List[Any]]) -> Dict[str, Any] | None:
        """Evaluate raw OHLCV candles and return a signal dict or *None*.

        Args:
            candles: List of ``[timestamp, open, high, low, close, volume]``.

        Returns:
            A :class:`StrategySignal` if a trade signal is detected,
            or ``None`` if no action should be taken.
        """

    @abstractmethod
    def get_required_candle_count(self) -> int:
        """Return the minimum number of historical candles this strategy needs."""

    @abstractmethod
    def get_name(self) -> str:
        """Return a human-readable name for this strategy."""
