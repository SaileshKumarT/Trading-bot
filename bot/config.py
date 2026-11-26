"""Configuration loader and validator.

Reads every setting from environment variables (populated by .env)
and exposes them as typed attributes on a single `Config` dataclass.
Validates required values on import so the bot fails fast on misconfiguration.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

# Load .env file from project root
load_dotenv()


def _require(key: str) -> str:
    """Return an environment variable or exit with a clear error."""
    value = os.getenv(key)
    if not value:
        print(f"[FATAL] Missing required environment variable: {key}")
        sys.exit(1)
    return value


def _get_float(key: str, default: float) -> float:
    """Return an environment variable as float, with a fallback default."""
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[FATAL] Invalid float for {key}: {raw}")
        sys.exit(1)


def _get_int(key: str, default: int) -> int:
    """Return an environment variable as int, with a fallback default."""
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[FATAL] Invalid int for {key}: {raw}")
        sys.exit(1)


def _get_bool(key: str, default: bool) -> bool:
    """Return an environment variable as bool."""
    raw = os.getenv(key, str(default)).lower()
    return raw in ("true", "1", "yes")


@dataclass(frozen=True)
class Config:
    """Immutable application configuration populated from environment."""

    # ── Exchange ────────────────────────────────────────────
    delta_api_key: str = field(default_factory=lambda: _require("DELTA_API_KEY"))
    delta_api_secret: str = field(default_factory=lambda: _require("DELTA_API_SECRET"))
    testnet_base_url: str = field(
        default_factory=lambda: os.getenv(
            "DELTA_BASE_URL", "https://cdn-ind.testnet.deltaex.org"
        )
    )

    # ── Telegram ────────────────────────────────────────────
    telegram_bot_token: str = field(default_factory=lambda: _require("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _require("TELEGRAM_CHAT_ID"))

    # ── Database ────────────────────────────────────────────
    database_url: str = field(
        default_factory=lambda: _require("DATABASE_URL")
    )

    # ── Trading ─────────────────────────────────────────────
    trading_symbol: str = field(
        default_factory=lambda: os.getenv("TRADING_SYMBOL", "BTCUSD")
    )
    timeframe: str = field(
        default_factory=lambda: os.getenv("TIMEFRAME", "15m")
    )
    capital: float = field(default_factory=lambda: _get_float("CAPITAL", 10000.0))
    risk_per_trade: float = field(default_factory=lambda: _get_float("RISK_PER_TRADE", 0.02))
    leverage: int = field(default_factory=lambda: _get_int("LEVERAGE", 5))
    stop_loss_pct: float = field(default_factory=lambda: _get_float("STOP_LOSS_PCT", 0.015))
    take_profit_pct: float = field(default_factory=lambda: _get_float("TAKE_PROFIT_PCT", 0.03))
    max_open_positions: int = field(default_factory=lambda: _get_int("MAX_OPEN_POSITIONS", 2))
    daily_loss_limit: float = field(default_factory=lambda: _get_float("DAILY_LOSS_LIMIT", 0.05))
    maker_fee: float = field(default_factory=lambda: _get_float("MAKER_FEE", 0.0002))

    # ── Strategy Selection ──────────────────────────────────
    active_strategy: str = field(
        default_factory=lambda: os.getenv("ACTIVE_STRATEGY", "ema_crossover_rsi")
    )

    # ── Strategy Parameters (EMA Crossover + RSI) ──────────
    ema_fast_period: int = field(default_factory=lambda: _get_int("EMA_FAST_PERIOD", 9))
    ema_slow_period: int = field(default_factory=lambda: _get_int("EMA_SLOW_PERIOD", 21))
    rsi_period: int = field(default_factory=lambda: _get_int("RSI_PERIOD", 14))
    rsi_buy_low: float = field(default_factory=lambda: _get_float("RSI_BUY_LOW", 40.0))
    rsi_buy_high: float = field(default_factory=lambda: _get_float("RSI_BUY_HIGH", 65.0))
    rsi_sell_low: float = field(default_factory=lambda: _get_float("RSI_SELL_LOW", 35.0))
    rsi_sell_high: float = field(default_factory=lambda: _get_float("RSI_SELL_HIGH", 60.0))

    # ── Spread Filter ──────────────────────────────────────
    spread_max_pct: float = field(default_factory=lambda: _get_float("SPREAD_MAX_PCT", 0.005))

    # ── Order Management ────────────────────────────────────
    order_expiry_candles: int = field(default_factory=lambda: _get_int("ORDER_EXPIRY_CANDLES", 3))
    max_retries: int = field(default_factory=lambda: _get_int("MAX_RETRIES", 3))

    # ── Logging ─────────────────────────────────────────────
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper()
    )

    # ── Kill Switch ─────────────────────────────────────────
    kill_switch: bool = field(default_factory=lambda: _get_bool("KILL_SWITCH", False))


# Singleton instance — import this everywhere
cfg = Config()
