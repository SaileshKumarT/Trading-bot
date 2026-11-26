"""Trading strategies package.

Provides a pluggable strategy system with a common base class,
concrete strategy implementations, and a registry for runtime selection.
"""

from bot.strategies.base_strategy import BaseStrategy
from bot.strategies.registry import get_strategy, STRATEGY_REGISTRY

__all__ = ["BaseStrategy", "get_strategy", "STRATEGY_REGISTRY"]
