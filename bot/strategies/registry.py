"""Strategy registry — maps strategy name strings to their classes."""

from __future__ import annotations

import sys
from typing import Any, Dict, Type

from bot.strategies.base_strategy import BaseStrategy
from bot.strategies.ema_crossover_rsi import EMACrossoverRSI
from bot.strategies.bollinger_mean_revert import BollingerMeanRevert
from bot.strategies.macd_momentum import MACDMomentum

STRATEGY_REGISTRY: Dict[str, Type[BaseStrategy]] = {
    "ema_crossover_rsi": EMACrossoverRSI,
    "bollinger_mean_revert": BollingerMeanRevert,
    "macd_momentum": MACDMomentum,
}


def get_strategy(name: str, config: Any) -> BaseStrategy:
    """Look up a strategy by name, instantiate it, and return it.

    Args:
        name: Strategy identifier (must be a key in STRATEGY_REGISTRY).
        config: The application Config instance passed to the strategy.

    Returns:
        An instantiated BaseStrategy subclass.

    Raises:
        SystemExit: If the strategy name is not found (exits with code 1).
    """
    cls = STRATEGY_REGISTRY.get(name)
    if cls is None:
        available = ", ".join(sorted(STRATEGY_REGISTRY.keys()))
        print(
            f"[FATAL] Unknown strategy: '{name}'. "
            f"Available strategies: {available}"
        )
        sys.exit(1)
    return cls(config)
