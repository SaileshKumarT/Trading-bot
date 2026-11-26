"""Delta Exchange testnet WebSocket client.

Connects to the Delta Exchange testnet WebSocket, subscribes to public
and private channels, and maintains latest market/account state in memory.

Standalone test usage:
    python -m bot.ws_client
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import websockets
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    InvalidStatus,
)

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ── Configuration from environment ────────────────────────────

_API_KEY = os.getenv("DELTA_API_KEY", "")
_API_SECRET = os.getenv("DELTA_API_SECRET", "")
_TRADING_SYMBOL = os.getenv("TRADING_SYMBOL", "BTCUSD")
_TIMEFRAME = os.getenv("TIMEFRAME", "1m")
_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# WebSocket URL: Delta uses a separate socket subdomain, not the REST host.
# Production India: wss://socket.india.delta.exchange
# Testnet India:    wss://testnet-socket.delta.exchange
_WS_URL = os.getenv("DELTA_WS_URL", "wss://testnet-socket.delta.exchange")

# Reconnection parameters
_RECONNECT_BASE_DELAY = 1.0     # seconds
_RECONNECT_MAX_DELAY = 60.0     # seconds
_HEARTBEAT_INTERVAL = 25.0      # seconds between pings


# ── In-memory state ───────────────────────────────────────────

@dataclass
class MarketState:
    """Thread-safe-ish snapshot of latest market and account data."""

    # Orderbook (L2)
    best_bid: Optional[float] = None
    best_bid_size: Optional[float] = None
    best_ask: Optional[float] = None
    best_ask_size: Optional[float] = None
    mid_price: Optional[float] = None

    # Latest candle
    candle_open: Optional[float] = None
    candle_high: Optional[float] = None
    candle_low: Optional[float] = None
    candle_close: Optional[float] = None
    candle_volume: Optional[float] = None
    candle_ts: Optional[int] = None

    # Mark price
    mark_price: Optional[float] = None

    # User positions  (list of dicts from exchange)
    positions: List[Dict[str, Any]] = field(default_factory=list)

    # User open orders (list of dicts from exchange)
    orders: List[Dict[str, Any]] = field(default_factory=list)

    # Last update timestamps
    orderbook_ts: Optional[float] = None
    candle_update_ts: Optional[float] = None
    position_update_ts: Optional[float] = None
    order_update_ts: Optional[float] = None


# Singleton state object
state = MarketState()


# ── Authentication helper ─────────────────────────────────────

def _make_auth_payload() -> Dict[str, Any]:
    """Build the WebSocket authentication message.

    Delta uses ``key-auth`` (the old ``auth`` type is deprecated).
    Signature = HMAC-SHA256(secret, "GET" + timestamp + "/live")
    Fields go inside ``payload`` with hyphenated ``api-key``.
    """
    timestamp = str(int(time.time()))
    signature_data = "GET" + timestamp + "/live"
    signature = hmac.new(
        _API_SECRET.encode(),
        signature_data.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "type": "key-auth",
        "payload": {
            "api-key": _API_KEY,
            "signature": signature,
            "timestamp": timestamp,
        },
    }


# ── Message handlers ──────────────────────────────────────────

def _handle_l2_orderbook(data: Dict[str, Any]) -> None:
    """Update best bid/ask/mid from L2 orderbook snapshot."""
    try:
        buy = data.get("buy", [])
        sell = data.get("sell", [])
        if buy:
            # buy orders sorted descending by price — first is best bid
            best = buy[0]
            state.best_bid = float(best.get("limit_price", best.get("price", 0)))
            state.best_bid_size = float(best.get("size", 0))
        if sell:
            # sell orders sorted ascending by price — first is best ask
            best = sell[0]
            state.best_ask = float(best.get("limit_price", best.get("price", 0)))
            state.best_ask_size = float(best.get("size", 0))
        if state.best_bid and state.best_ask:
            state.mid_price = (state.best_bid + state.best_ask) / 2.0
        state.orderbook_ts = time.time()
        logger.info(
            "OB  bid=%.2f(%s)  ask=%.2f(%s)  mid=%.2f",
            state.best_bid or 0, state.best_bid_size or 0,
            state.best_ask or 0, state.best_ask_size or 0,
            state.mid_price or 0,
        )
    except Exception:
        logger.exception("Error parsing l2_orderbook")


def _handle_candlestick(data: Dict[str, Any]) -> None:
    """Update latest candle from candlestick channel."""
    try:
        state.candle_open = float(data.get("open", 0))
        state.candle_high = float(data.get("high", 0))
        state.candle_low = float(data.get("low", 0))
        state.candle_close = float(data.get("close", 0))
        state.candle_volume = float(data.get("volume", 0))
        state.candle_ts = data.get("timestamp") or data.get("candle_start_time")
        state.candle_update_ts = time.time()
        logger.info(
            "CANDLE  O=%.2f H=%.2f L=%.2f C=%.2f V=%.2f",
            state.candle_open, state.candle_high,
            state.candle_low, state.candle_close,
            state.candle_volume,
        )
    except Exception:
        logger.exception("Error parsing candlestick")


def _handle_mark_price(data: Dict[str, Any]) -> None:
    """Update mark price."""
    try:
        state.mark_price = float(data.get("mark_price", 0))
        logger.info("MARK  %.2f", state.mark_price)
    except Exception:
        logger.exception("Error parsing mark_price")


def _handle_positions(data: Any) -> None:
    """Update user positions from private channel."""
    try:
        if isinstance(data, list):
            state.positions = data
        elif isinstance(data, dict):
            state.positions = [data]
        state.position_update_ts = time.time()
        logger.info("POSITIONS  count=%d  %s", len(state.positions), state.positions)
    except Exception:
        logger.exception("Error parsing positions")


def _handle_orders(data: Any) -> None:
    """Update user orders from private channel."""
    try:
        if isinstance(data, list):
            state.orders = data
        elif isinstance(data, dict):
            state.orders = [data]
        state.order_update_ts = time.time()
        logger.info("ORDERS  count=%d  %s", len(state.orders), state.orders)
    except Exception:
        logger.exception("Error parsing orders")


# Channel name → handler mapping
# Note: candlestick channel name includes the resolution, e.g. "candlestick_1m"
_HANDLERS = {
    "l2_orderbook": _handle_l2_orderbook,
    "l1_orderbook": _handle_l2_orderbook,
    "mark_price": _handle_mark_price,
    "positions": _handle_positions,
    "orders": _handle_orders,
}


# ── Core WebSocket loop ──────────────────────────────────────

async def _subscribe(ws) -> None:
    """Send subscription messages for all desired channels.

    Delta Exchange uses the nested format:
      {"type": "subscribe", "payload": {"channels": [{"name": ..., "symbols": [...]}]}}

    Private channels also require ``"symbols"`` (use the trading symbol
    or ``"all"`` for every product).  Auth must succeed before subscribing.
    """
    symbol = _TRADING_SYMBOL

    # ── Public channels ───────────────────────────────────────
    # Public: L2 orderbook
    await ws.send(json.dumps({
        "type": "subscribe",
        "payload": {
            "channels": [{"name": "l2_orderbook", "symbols": [symbol]}],
        },
    }))
    logger.info("Subscribing → l2_orderbook [%s]", symbol)

    # Public: candlesticks (channel name embeds the resolution)
    await ws.send(json.dumps({
        "type": "subscribe",
        "payload": {
            "channels": [{
                "name": "candlestick_" + _TIMEFRAME,
                "symbols": [symbol],
            }],
        },
    }))
    logger.info("Subscribing → candlestick_%s [%s]", _TIMEFRAME, symbol)

    # Public: mark price
    await ws.send(json.dumps({
        "type": "subscribe",
        "payload": {
            "channels": [{"name": "mark_price", "symbols": [symbol]}],
        },
    }))
    logger.info("Subscribing → mark_price [%s]", symbol)

    # Public: v2 ticker
    await ws.send(json.dumps({
        "type": "subscribe",
        "payload": {
            "channels": [{"name": "v2/ticker", "symbols": [symbol]}],
        },
    }))
    logger.info("Subscribing → v2/ticker [%s]", symbol)

    # ── Authenticate, then private channels ───────────────────
    if _API_KEY and _API_SECRET:
        auth = _make_auth_payload()
        await ws.send(json.dumps(auth))
        logger.info("Sent key-auth message — waiting for confirmation…")

        # Wait for the auth response before subscribing to private channels.
        # Read messages until we get the key-auth confirmation (or timeout).
        auth_ok = False
        for _ in range(20):  # up to ~10 s
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                msg = json.loads(raw)
                _dispatch(raw)  # process normally (may be a public msg)
                if msg.get("type") == "key-auth":
                    if msg.get("success"):
                        logger.info("Auth succeeded")
                        auth_ok = True
                        break
                    else:
                        logger.error("Auth FAILED: %s", msg)
                        break
            except asyncio.TimeoutError:
                continue

        if not auth_ok:
            logger.warning("Auth not confirmed — private channels may fail")

        # Private: positions, orders, margins — each needs symbols
        await ws.send(json.dumps({
            "type": "subscribe",
            "payload": {
                "channels": [
                    {"name": "positions", "symbols": [symbol]},
                    {"name": "orders", "symbols": [symbol]},
                    {"name": "margins", "symbols": [symbol]},
                ],
            },
        }))
        logger.info("Subscribing → positions, orders, margins [%s] (private)", symbol)
    else:
        logger.warning("No API credentials — skipping private channels")


async def _heartbeat(ws) -> None:
    """Send periodic pings to keep the connection alive."""
    while True:
        try:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            await ws.send(json.dumps({"type": "heartbeat"}))
            logger.debug("Sent heartbeat")
        except (ConnectionClosed, ConnectionClosedError):
            break
        except Exception:
            logger.debug("Heartbeat send failed", exc_info=True)
            break


def _dispatch(raw: str) -> None:
    """Parse an incoming message and route it to the appropriate handler."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Non-JSON message: %s", raw[:200])
        return

    logger.debug("WS ← %s", raw[:500])

    msg_type = msg.get("type", "")

    # Subscription confirmations — log each channel, flag errors
    if msg_type == "subscriptions":
        for ch in msg.get("channels", []):
            name = ch.get("name", "?")
            syms = ",".join(ch.get("symbols", []))
            err = ch.get("error")
            if err:
                logger.error("Subscribe FAILED  %-20s  %s", name, err)
            else:
                logger.info("Subscribe OK      %-20s  [%s]", name, syms)
        return

    # Auth response
    if msg_type == "key-auth":
        if msg.get("success"):
            logger.info("Auth confirmed ✓")
        else:
            logger.error("Auth failed: %s", msg)
        return

    # Error messages
    if msg_type == "error":
        logger.error("WS error: %s", msg.get("message", msg))
        return

    # Other control messages
    if msg_type in ("subscribed", "unsubscribed"):
        logger.info("WS [%s] %s", msg_type, msg)
        return

    # Heartbeat responses
    if msg_type == "heartbeat":
        logger.debug("Heartbeat ACK")
        return

    # Data messages — look up handler by channel name
    channel = msg.get("channel", "").lower()
    data = msg.get("data", msg)

    handler = _HANDLERS.get(channel)
    # Candlestick channels include resolution suffix, e.g. "candlestick_1m"
    if handler is None and channel.startswith("candlestick_"):
        handler = _handle_candlestick
    if handler:
        handler(data)
    else:
        logger.debug("Unhandled channel=%s type=%s", channel, msg_type)


async def run(stop_event: Optional[asyncio.Event] = None) -> None:
    """Main loop: connect → subscribe → listen → reconnect on failure.

    Uses exponential backoff for reconnection.  Pass an asyncio.Event
    as *stop_event* to allow graceful shutdown from outside.
    """
    attempt = 0

    while True:
        if stop_event and stop_event.is_set():
            logger.info("Stop event set — exiting WS loop")
            return

        delay = min(_RECONNECT_BASE_DELAY * (2 ** attempt), _RECONNECT_MAX_DELAY)

        try:
            logger.info("Connecting to %s (attempt %d)…", _WS_URL, attempt + 1)
            async with websockets.connect(
                _WS_URL,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=5,
            ) as ws:
                logger.info("Connected to Delta Exchange WebSocket")
                attempt = 0  # reset backoff on successful connect

                # Subscribe to channels
                await _subscribe(ws)

                # Start heartbeat task
                hb_task = asyncio.create_task(_heartbeat(ws))

                try:
                    async for raw_msg in ws:
                        _dispatch(raw_msg)
                        if stop_event and stop_event.is_set():
                            break
                finally:
                    hb_task.cancel()
                    try:
                        await hb_task
                    except asyncio.CancelledError:
                        pass

        except (ConnectionClosed, ConnectionClosedError) as exc:
            logger.warning("WebSocket closed: %s — reconnecting in %.1fs", exc, delay)
        except (OSError, InvalidStatus) as exc:
            logger.warning("Connection error: %s — reconnecting in %.1fs", exc, delay)
        except asyncio.CancelledError:
            logger.info("WS task cancelled — exiting")
            return
        except Exception:
            logger.exception("Unexpected WS error — reconnecting in %.1fs", delay)

        attempt += 1
        await asyncio.sleep(delay)


# ── Standalone test entry point ───────────────────────────────

async def _main() -> None:
    """Run the WebSocket client as a standalone test."""
    # Configure logging for standalone mode
    logging.basicConfig(
        level=getattr(logging, _LOG_LEVEL, logging.DEBUG),
        format="%(asctime)s [%(levelname)-7s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    # Force our logger to DEBUG for maximum visibility during testing
    logging.getLogger(__name__).setLevel(logging.DEBUG)

    print("=" * 60)
    print("Delta Exchange WebSocket — Standalone Test")
    print("=" * 60)
    print(f"  WS URL    : {_WS_URL}")
    print(f"  Symbol    : {_TRADING_SYMBOL}")
    print(f"  Timeframe : {_TIMEFRAME}")
    print(f"  API Key   : {_API_KEY[:8]}…" if _API_KEY else "  API Key   : (none)")
    print(f"  Log Level : {_LOG_LEVEL}")
    print("=" * 60)
    print("Press Ctrl+C to stop.\n")

    stop = asyncio.Event()

    try:
        await run(stop_event=stop)
    except KeyboardInterrupt:
        print("\nStopping…")
        stop.set()


if __name__ == "__main__":
    asyncio.run(_main())
