"""Backward-compatible shim for the original strategy module.

The strategy logic has been moved to ``bot.strategies.ema_crossover_rsi``.
This module re-exports the original public names so that existing imports
(e.g. in ``bot.executor``) continue to work without changes.
"""

from bot.strategies.base_strategy import StrategySignal
from bot.strategies.ema_crossover_rsi import (
    compute_indicators,
    detect_crossover,
    EMACrossoverRSI,
)
from bot.config import cfg

__all__ = [
    "StrategySignal",
    "compute_indicators",
    "detect_crossover",
    "generate_signal",
]

# Singleton strategy instance for backward-compatible generate_signal()
_default_strategy = EMACrossoverRSI(cfg)


def generate_signal(candles):
    """Backward-compatible wrapper — delegates to the EMACrossoverRSI class."""
    return _default_strategy.evaluate(candles)
