"""WebSocket connection manager.

Maintains a set of active WebSocket connections and provides
broadcast methods for pushing live data and log lines to all
connected dashboard clients simultaneously.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections for a specific channel (live data or logs).

    Handles connect, disconnect, and broadcast to all active connections
    with automatic cleanup of stale/broken connections.
    """

    def __init__(self, channel_name: str = "default") -> None:
        """Initialise the connection manager.

        Args:
            channel_name: Human-readable name for logging (e.g. 'live', 'logs').
        """
        self._connections: Set[WebSocket] = set()
        self._channel_name = channel_name

    async def connect(self, websocket: WebSocket) -> None:
        """Accept a new WebSocket connection and add it to the pool.

        Args:
            websocket: The incoming WebSocket connection.
        """
        await websocket.accept()
        self._connections.add(websocket)
        logger.info(
            "WebSocket [%s] client connected. Active: %d",
            self._channel_name,
            len(self._connections),
        )

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection from the pool.

        Args:
            websocket: The disconnected WebSocket.
        """
        self._connections.discard(websocket)
        logger.info(
            "WebSocket [%s] client disconnected. Active: %d",
            self._channel_name,
            len(self._connections),
        )

    async def broadcast_json(self, data: Dict[str, Any]) -> None:
        """Send a JSON payload to all connected clients.

        Silently removes connections that have gone stale.

        Args:
            data: Dict to serialize as JSON and send.
        """
        if not self._connections:
            return

        message = json.dumps(data)
        stale: List[WebSocket] = []

        for ws in self._connections.copy():
            try:
                await ws.send_text(message)
            except Exception:
                stale.append(ws)

        for ws in stale:
            self._connections.discard(ws)

    async def broadcast_text(self, text: str) -> None:
        """Send a plain text message to all connected clients.

        Args:
            text: Text message to broadcast.
        """
        if not self._connections:
            return

        stale: List[WebSocket] = []

        for ws in self._connections.copy():
            try:
                await ws.send_text(text)
            except Exception:
                stale.append(ws)

        for ws in stale:
            self._connections.discard(ws)

    @property
    def active_count(self) -> int:
        """Return the number of active connections."""
        return len(self._connections)


# ── Module-level managers ──────────────────────────────────

live_manager = ConnectionManager("live")
logs_manager = ConnectionManager("logs")
