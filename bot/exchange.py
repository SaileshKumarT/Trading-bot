"""Delta Exchange India testnet — direct REST API wrapper.

Uses *only* the ``requests`` library with HMAC-SHA256 authentication.
No CCXT, no delta-rest-client — just raw HTTP to
https://cdn-ind.testnet.deltaex.org/v2/*.

Every public method keeps the same signature and return format that the
rest of the bot (executor, strategy, risk, main, pnl, dashboard) already
relies on, so **no other file needs to change**.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

from bot.config import cfg

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────

# Timeframe string → seconds per bar (used to compute candle window)
_BAR_SECONDS: Dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600,
    "1d": 86400, "1w": 604800,
}

# Delta order state → CCXT-style status expected by executor.py
_ORDER_STATUS_MAP: Dict[str, str] = {
    "open": "open",
    "pending": "open",
    "closed": "closed",
    "cancelled": "canceled",
    "canceled": "canceled",
}


def _build_symbol_aliases(symbol: str) -> List[str]:
    """Generate candidate Delta symbols from a user-supplied symbol string.

    Handles direct Delta symbols (``BTCUSD``), CCXT-style symbols
    (``BTC/USDT:USDT``), and common typos (``BTCUSDT``).

    Returns a list of candidates to try against the product map, ordered
    from most-likely to least-likely.
    """
    # Normalise: strip whitespace, uppercase
    s = symbol.strip().upper()

    candidates: List[str] = [s]  # always try the raw input first

    # Strip CCXT settle-asset suffix  "BTC/USDT:USDT" → "BTC/USDT"
    if ":" in s:
        s = s.split(":")[0]
        candidates.append(s)

    # Strip slashes  "BTC/USDT" → "BTCUSDT"
    no_slash = s.replace("/", "")
    if no_slash not in candidates:
        candidates.append(no_slash)

    # BTCUSDT → BTCUSD  (Delta India perps are quoted in USD, not USDT)
    if no_slash.endswith("USDT"):
        usd_variant = no_slash[:-1]          # BTCUSDT → BTCUSD
        if usd_variant not in candidates:
            candidates.append(usd_variant)

    # Also try just the base + USD in case input was "BTC"
    if "/" in s:
        base = s.split("/")[0]
        base_usd = base + "USD"
        if base_usd not in candidates:
            candidates.append(base_usd)

    return candidates


# ── Exchange class ───────────────────────────────────────────


class Exchange:
    """Direct REST wrapper for Delta Exchange India testnet.

    Authenticates via HMAC-SHA256 and talks to the v2 API.  Exposes the
    exact same public methods (with the same return shapes) that the
    previous CCXT-based class did, so callers need zero changes.
    """

    def __init__(self) -> None:
        """Initialise the HTTP session and auth credentials."""
        self._base_url: str = cfg.testnet_base_url.rstrip("/")
        self._api_key: str = cfg.delta_api_key
        self._api_secret: str = cfg.delta_api_secret

        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "delta-trading-bot/1.0",
        })

        # Populated by load_markets()
        self._products: Dict[str, Dict[str, Any]] = {}   # delta_symbol → full product dict
        self._product_ids: Dict[str, int] = {}            # delta_symbol → product_id
        self._contract_values: Dict[str, float] = {}      # delta_symbol → contract_value

        logger.info(
            "Exchange initialised: Delta Exchange India testnet (%s)",
            self._base_url,
        )

    # ── Low-level HTTP + auth ───────────────────────────────

    def _sign(
        self, method: str, path: str, body: str = "",
    ) -> Dict[str, str]:
        """Build Delta API authentication headers.

        Signature = HMAC-SHA256(secret, method + timestamp + path + body)
        For GET/DELETE with query params, path must include the query string.
        """
        timestamp = str(int(time.time()))
        message = method + timestamp + path + body
        signature = hmac.new(
            self._api_secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "api-key": self._api_key,
            "timestamp": timestamp,
            "signature": signature,
        }

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
        auth: bool = False,
    ) -> Any:
        """Send an HTTP request and return the parsed ``result`` field.

        Raises
        ------
        requests.exceptions.HTTPError
            On 4xx / 5xx responses (caught by ``_retry`` for transient ones).
        RuntimeError
            When the API returns ``{"success": false, …}``.
        """
        url = self._base_url + path
        query_string = ""
        body_str = ""

        if params:
            query_string = "?" + urlencode(params, doseq=True)
            url += query_string

        if payload is not None:
            body_str = json.dumps(payload, separators=(",", ":"))

        headers: Dict[str, str] = {}
        if auth:
            headers = self._sign(method, path + query_string, body_str)

        logger.debug(
            "API request: %s %s | query=%s | body=%s",
            method, path, query_string or "(none)", body_str or "(none)",
        )

        resp = self._session.request(
            method=method,
            url=url,
            headers=headers,
            data=body_str if payload is not None else None,
        )

        # Log non-2xx responses before raising so we can debug
        if not resp.ok:
            logger.warning(
                "API %s %s returned HTTP %d: %s",
                method, path, resp.status_code, resp.text[:500],
            )

        resp.raise_for_status()

        data = resp.json()
        if isinstance(data, dict) and data.get("success") is False:
            error = data.get("error", data)
            raise RuntimeError(f"Delta API error: {error}")

        # Most v2 endpoints wrap their payload under "result"
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return data

    # ── Symbol helpers ──────────────────────────────────────

    def _resolve_symbol(self, symbol: str) -> str:
        """Resolve any supported symbol format to the actual Delta symbol.

        Tries the input as-is first, then CCXT variations, then the
        USDT→USD fallback.  Raises ``ValueError`` if nothing matches.
        """
        for candidate in _build_symbol_aliases(symbol):
            if candidate in self._products:
                return candidate
        raise ValueError(
            f"Unknown symbol {symbol!r} — tried {_build_symbol_aliases(symbol)}. "
            f"Call load_markets() first or check TRADING_SYMBOL in .env."
        )

    def _get_product_id(self, symbol: str) -> int:
        """Resolve any symbol format to a Delta numeric product_id."""
        delta_sym = self._resolve_symbol(symbol)
        return self._product_ids[delta_sym]

    def _get_delta_symbol(self, symbol: str) -> str:
        """Resolve any symbol format to the actual Delta symbol string."""
        return self._resolve_symbol(symbol)

    def _get_product(self, symbol: str) -> Dict[str, Any]:
        """Return the full product dict for a symbol."""
        delta_sym = self._resolve_symbol(symbol)
        return self._products[delta_sym]

    def _get_contract_value(self, symbol: str) -> float:
        """Return the contract_value for a symbol (e.g. 0.001 BTC per contract)."""
        delta_sym = self._resolve_symbol(symbol)
        return self._contract_values.get(delta_sym, 1.0)

    def _amount_to_contracts(self, symbol: str, amount: float) -> int:
        """Convert a base-currency amount to whole contracts.

        Args:
            symbol: Trading pair.
            amount: Size in base currency (e.g. 0.014823 BTC).

        Returns:
            Number of contracts (minimum 1).
        """
        cv = self._get_contract_value(symbol)
        contracts = max(1, round(amount / cv))
        logger.debug(
            "Order size: %.6f BTC → %d contracts (contract_value=%s)",
            amount, contracts, cv,
        )
        return contracts

    # ── Market loading ──────────────────────────────────────

    def load_markets(self) -> None:
        """Fetch ``/v2/products`` and cache every product by symbol."""
        products = self._retry(self._request, "GET", "/v2/products")
        if isinstance(products, list):
            # Debug: show a sample of perpetual futures symbols
            perps = [p.get("symbol", "?") for p in products
                     if p.get("contract_type") == "perpetual_futures"]
            logger.info("Delta perpetual futures symbols: %s", perps)
            for p in products:
                sym = p.get("symbol", "")
                if sym:
                    self._products[sym] = p
                    self._product_ids[sym] = p["id"]
                    cv = float(p.get("contract_value", 1))
                    self._contract_values[sym] = cv
        logger.info("Markets loaded: %d symbols available.", len(self._products))
        # Verify the configured trading symbol resolves
        try:
            delta_sym = self._resolve_symbol(cfg.trading_symbol)
            pid = self._product_ids[delta_sym]
            cv = self._contract_values[delta_sym]
            logger.info(
                "Trading symbol '%s' resolved to '%s' (product_id=%d, contract_value=%s).",
                cfg.trading_symbol, delta_sym, pid, cv,
            )
        except ValueError as exc:
            logger.error("Symbol resolution failed: %s", exc)

    # ── OHLCV / Candles ─────────────────────────────────────

    def fetch_ohlcv(
        self,
        symbol: str = cfg.trading_symbol,
        timeframe: str = cfg.timeframe,
        limit: int = 100,
    ) -> List[List[Any]]:
        """Fetch recent OHLCV candle data.

        Returns:
            List of ``[timestamp_ms, open, high, low, close, volume]`` lists.
        """
        delta_sym = self._get_delta_symbol(symbol)
        bar_secs = _BAR_SECONDS.get(timeframe, 900)
        now = int(time.time())
        start = now - (bar_secs * limit)

        raw = self._retry(
            self._request, "GET", "/v2/history/candles",
            params={
                "resolution": timeframe,
                "symbol": delta_sym,
                "start": str(start),
                "end": str(now),
            },
        )
        candles = [
            [
                int(c["time"]) * 1000,
                float(c["open"]),
                float(c["high"]),
                float(c["low"]),
                float(c["close"]),
                float(c.get("volume", 0)),
            ]
            for c in (raw if isinstance(raw, list) else [])
        ]
        candles.sort(key=lambda x: x[0])
        logger.debug("Fetched %d candles for %s [%s].", len(candles), symbol, timeframe)
        return candles

    # ── Order book / Ticker ─────────────────────────────────

    def fetch_order_book(self, symbol: str = cfg.trading_symbol) -> Dict[str, Any]:
        """Fetch the L2 order book.

        Returns:
            ``{"bids": [[price, size], …], "asks": [[price, size], …]}``
        """
        delta_sym = self._get_delta_symbol(symbol)
        raw = self._retry(
            self._request, "GET", f"/v2/l2orderbook/{delta_sym}",
        )
        bids = [
            [float(e["price"]), float(e["size"])]
            for e in raw.get("buy", [])
        ]
        asks = [
            [float(e["price"]), float(e["size"])]
            for e in raw.get("sell", [])
        ]
        return {"bids": bids, "asks": asks}

    def get_bid_ask(self, symbol: str = cfg.trading_symbol) -> Dict[str, float]:
        """Return best bid, best ask, and mid-price.

        Returns:
            ``{"bid": float, "ask": float, "mid": float}``
        """
        book = self.fetch_order_book(symbol)
        best_bid = book["bids"][0][0] if book["bids"] else 0
        best_ask = book["asks"][0][0] if book["asks"] else 0
        mid = (best_bid + best_ask) / 2
        logger.debug(
            "Bid/Ask for %s: bid=%.2f, ask=%.2f, mid=%.2f",
            symbol, best_bid, best_ask, mid,
        )
        return {"bid": best_bid, "ask": best_ask, "mid": mid}

    def get_mid_price(self, symbol: str = cfg.trading_symbol) -> float:
        """Return the midpoint between best bid and best ask."""
        return self.get_bid_ask(symbol)["mid"]

    def fetch_ticker(self, symbol: str = cfg.trading_symbol) -> Dict[str, Any]:
        """Fetch 24 h ticker data.

        Returns:
            CCXT-style dict — callers use ``ticker.get("percentage")``.
        """
        delta_sym = self._get_delta_symbol(symbol)
        raw = self._retry(
            self._request, "GET", f"/v2/tickers/{delta_sym}",
        )
        open_price = float(raw.get("open") or 0)
        close_price = float(raw.get("close") or 0)
        pct = ((close_price - open_price) / open_price * 100) if open_price else 0
        return {
            "symbol": symbol,
            "last": close_price,
            "open": open_price,
            "high": float(raw.get("high") or 0),
            "low": float(raw.get("low") or 0),
            "close": close_price,
            "bid": float(raw.get("best_bid") or 0) or None,
            "ask": float(raw.get("best_ask") or 0) or None,
            "baseVolume": float(raw.get("volume") or 0),
            "percentage": pct,
            "markPrice": float(raw.get("mark_price") or 0),
            "info": raw,
        }

    # ── Wallet / Balance ────────────────────────────────────

    def fetch_balance(self) -> Dict[str, Any]:
        """Fetch wallet balances and return CCXT-style dict.

        Returns:
            ``{"USDT": {"total": float, "free": float, "used": float}, …}``
        """
        wallets = self._retry(
            self._request, "GET", "/v2/wallet/balances", auth=True,
        )
        result: Dict[str, Any] = {}
        for w in (wallets if isinstance(wallets, list) else []):
            asset = w.get("asset_symbol", w.get("asset_id", ""))
            balance = float(w.get("balance", 0))
            available = float(w.get("available_balance", 0))
            result[str(asset)] = {
                "total": balance,
                "free": available,
                "used": balance - available,
            }
        return result

    # ── Order helpers ───────────────────────────────────────

    def _map_order(self, raw: Dict[str, Any], symbol: str = "") -> Dict[str, Any]:
        """Convert a Delta order dict to CCXT-style order dict.

        Fields the callers read:
            ``id``, ``status``, ``price``, ``average``, ``filled``, ``amount``, ``side``
        """
        size = float(raw.get("size", 0))
        unfilled = float(raw.get("unfilled_size", 0))
        state = raw.get("state", raw.get("status", ""))
        avg = raw.get("average_fill_price")
        return {
            "id": str(raw.get("id", "")),
            "symbol": symbol,
            "side": raw.get("side", ""),
            "price": float(raw.get("limit_price") or raw.get("price") or 0),
            "amount": size,
            "filled": size - unfilled,
            "remaining": unfilled,
            "average": float(avg) if avg else None,
            "status": _ORDER_STATUS_MAP.get(state, state),
            "type": "limit",
            "info": raw,
        }

    # ── Order Management ────────────────────────────────────

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Place a limit order.

        Args:
            symbol: Trading pair.
            side: ``'buy'`` or ``'sell'``.
            amount: Size in **base currency** (e.g. BTC).  Automatically
                converted to whole contracts using the product's
                ``contract_value``.
            price: Limit price.
            params: Extra parameters.  Supports ``reduceOnly``,
                ``postOnly``, and ``_is_contracts`` (if ``True``,
                *amount* is already a contract count and is not
                converted).

        Returns:
            CCXT-style order dict (callers read ``order["id"]``).
        """
        if params is None:
            params = {}
        product_id = self._get_product_id(symbol)

        # Convert base-currency amount → contracts, unless the caller
        # already passed a contract count (close_position does this).
        if params.get("_is_contracts"):
            contracts = max(1, int(amount))
        else:
            contracts = self._amount_to_contracts(symbol, amount)

        body: Dict[str, Any] = {
            "product_id": product_id,
            "size": contracts,
            "side": side,
            "limit_price": str(price),
            "order_type": "limit_order",
        }
        if params.get("reduceOnly"):
            body["reduce_only"] = True
        if params.get("postOnly"):
            body["post_only"] = True

        raw = self._retry(
            self._request, "POST", "/v2/orders", payload=body, auth=True,
        )
        order = self._map_order(raw, symbol)
        logger.info(
            "Limit order placed: %s %s %d contracts @ %.2f | id=%s",
            side.upper(), symbol, contracts, price, order.get("id"),
        )
        return order

    def place_stop_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        stop_price: float,
        stop_order_type: str,
        limit_price: Optional[float] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Place a stop order (stop-loss or take-profit) on Delta Exchange.

        Args:
            symbol: Trading pair.
            side: ``'buy'`` or ``'sell'`` — the side to execute when triggered.
            amount: Size.  Treated as contracts if ``params["_is_contracts"]``
                is truthy, otherwise converted from base currency.
            stop_price: The trigger price.
            stop_order_type: ``'stop_loss_order'`` or ``'take_profit_order'``.
            limit_price: If provided, the order is a stop-limit (fills at
                this price once triggered).  If ``None``, a stop-market
                order is placed.
            params: Extra parameters (``_is_contracts``, etc.).

        Returns:
            CCXT-style order dict.
        """
        if params is None:
            params = {}
        product_id = self._get_product_id(symbol)

        if params.get("_is_contracts"):
            contracts = max(1, int(amount))
        else:
            contracts = self._amount_to_contracts(symbol, amount)

        body: Dict[str, Any] = {
            "product_id": product_id,
            "size": contracts,
            "side": side,
            "stop_order_type": stop_order_type,
            "stop_price": str(stop_price),
            "reduce_only": True,
        }

        if limit_price is not None:
            body["order_type"] = "limit_order"
            body["limit_price"] = str(limit_price)
        else:
            body["order_type"] = "market_order"

        raw = self._retry(
            self._request, "POST", "/v2/orders", payload=body, auth=True,
        )
        order = self._map_order(raw, symbol)
        label = "Stop-loss" if "stop_loss" in stop_order_type else "Take-profit"
        logger.info(
            "%s order placed: %s %s %d contracts, stop=%.2f%s | id=%s",
            label, side.upper(), symbol, contracts, stop_price,
            f", limit={limit_price}" if limit_price else " (market)",
            order.get("id"),
        )
        return order

    def cancel_order(
        self, order_id: str, symbol: str = cfg.trading_symbol,
    ) -> Dict[str, Any]:
        """Cancel a single open order.

        Sends ``DELETE /v2/orders`` with ``id`` and ``product_id`` as
        query parameters (Delta's HMAC signature excludes the body for
        DELETE requests, so query params are the only signed channel).

        Gracefully handles orders that are already cancelled, filled,
        or no longer exist — logs a warning instead of raising.

        Returns:
            CCXT-style order dict, or ``{"id": order_id, "status": "canceled"}``
            when the order was already gone.
        """
        product_id = self._get_product_id(symbol)

        logger.info(
            "Cancelling order: id=%s, product_id=%d, symbol=%s",
            order_id, product_id, symbol,
        )

        try:
            raw = self._retry(
                self._request, "DELETE", "/v2/orders",
                params={"id": int(order_id), "product_id": product_id},
                auth=True,
            )
            logger.info("Order cancelled successfully: id=%s", order_id)
            if isinstance(raw, dict):
                return self._map_order(raw, symbol)
            return raw

        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            body = ""
            if exc.response is not None:
                try:
                    body = exc.response.text[:500]
                except Exception:
                    pass

            # 400/404 with "not found" or "already" → order is gone
            if status in (400, 404):
                body_lower = body.lower()
                if any(kw in body_lower for kw in (
                    "not_found", "not found", "already", "cancelled",
                    "canceled", "invalid", "does not exist",
                )):
                    logger.warning(
                        "Order %s already cancelled/filled/gone (HTTP %d): %s",
                        order_id, status, body,
                    )
                    return {"id": order_id, "status": "canceled"}

                # Unknown 400 — could be a real problem, log and raise
                logger.error(
                    "Cancel order %s failed with HTTP %d: %s",
                    order_id, status, body,
                )

            raise

        except RuntimeError as exc:
            # Delta API error with success=false
            err_lower = str(exc).lower()
            if any(kw in err_lower for kw in (
                "not_found", "not found", "already", "cancelled",
                "canceled", "does not exist",
            )):
                logger.warning(
                    "Order %s already cancelled/filled/gone: %s", order_id, exc,
                )
                return {"id": order_id, "status": "canceled"}
            raise

    def cancel_all_orders(
        self, symbol: str = cfg.trading_symbol,
    ) -> List[Dict[str, Any]]:
        """Cancel every open order for a product.

        Uses ``DELETE /v2/orders/all`` with the product_id as a query
        parameter (consistent with Delta's DELETE signing convention).

        Returns:
            List of cancellation responses (may be empty).
        """
        try:
            product_id = self._get_product_id(symbol)
            result = self._retry(
                self._request, "DELETE", "/v2/orders/all",
                params={"product_id": product_id},
                auth=True,
            )
            logger.info("All open orders cancelled for %s.", symbol)
            if isinstance(result, list):
                return result
            return [result] if result else []
        except Exception as exc:
            logger.warning("cancel_all_orders failed (may have no open orders): %s", exc)
            return []

    def fetch_order(
        self, order_id: str, symbol: str = cfg.trading_symbol,
    ) -> Dict[str, Any]:
        """Fetch the current state of an order.

        Returns:
            CCXT-style order dict with ``status``, ``average``, ``filled``.
        """
        raw = self._retry(
            self._request, "GET", f"/v2/orders/{order_id}", auth=True,
        )
        return self._map_order(raw, symbol)

    def fetch_open_orders(
        self, symbol: str = cfg.trading_symbol,
    ) -> List[Dict[str, Any]]:
        """Fetch all open orders, optionally filtered to a product.

        Returns:
            List of CCXT-style order dicts.
        """
        product_id = self._get_product_id(symbol)
        raw = self._retry(
            self._request, "GET", "/v2/orders",
            params={"state": "open", "product_id": str(product_id)},
            auth=True,
        )
        orders = raw if isinstance(raw, list) else []
        return [self._map_order(o, symbol) for o in orders]

    # ── Positions ───────────────────────────────────────────

    def fetch_positions(
        self, symbols: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch open positions.

        Delta ``GET /v2/positions`` returns a **single object** (not a
        list) and has **no ``side`` field**.  The sign of ``size``
        determines direction: positive = long, negative = short.

        Returns:
            List of dicts with ``contracts`` (float) and ``side``
            (``"long"`` or ``"short"``).
        """
        results: List[Dict[str, Any]] = []
        target_symbols = symbols or [cfg.trading_symbol]

        for sym in target_symbols:
            product_id = self._get_product_id(sym)
            raw = self._retry(
                self._request, "GET", "/v2/positions",
                params={"product_id": str(product_id)},
                auth=True,
            )

            logger.debug("Raw position response for %s (product_id=%d): %s", sym, product_id, raw)

            # API returns a single dict, not a list
            if isinstance(raw, list):
                positions = raw
            elif isinstance(raw, dict):
                positions = [raw]
            else:
                positions = []

            for p in positions:
                if not isinstance(p, dict):
                    continue

                raw_size = float(p.get("size", 0))

                if raw_size == 0:
                    logger.debug("Position for %s has size=0, skipping.", sym)
                    continue

                # Delta has no "side" field — derive from sign of size
                # positive size = long, negative size = short
                abs_size = abs(raw_size)
                if raw_size > 0:
                    ccxt_side = "long"
                else:
                    ccxt_side = "short"

                entry_price = float(p.get("entry_price", 0))

                logger.info(
                    "Position found: %s side=%s, contracts=%.0f, entry=%.2f "
                    "(raw size=%+.0f, product_id=%s)",
                    sym, ccxt_side, abs_size, entry_price,
                    raw_size, p.get("product_id", "?"),
                )

                results.append({
                    "symbol": sym,
                    "contracts": abs_size,
                    "side": ccxt_side,
                    "entryPrice": entry_price,
                    "unrealizedPnl": float(p.get("realized_pnl", 0)),
                    "info": p,
                })
        return results

    def close_position(
        self, symbol: str, side: str, amount: float,
    ) -> Dict[str, Any]:
        """Close an open position with a reduce-only limit order near mid-price.

        Args:
            symbol: Trading pair.
            side: Side of the *existing* position (``'buy'`` = long).
            amount: Number of **contracts** to close (from ``fetch_positions``).

        Returns:
            Order response dict.
        """
        close_side = "sell" if side == "buy" else "buy"
        mid = self.get_mid_price(symbol)
        if close_side == "buy":
            price = round(mid * 1.002, 2)
        else:
            price = round(mid * 0.998, 2)

        order = self.place_limit_order(
            symbol, close_side, amount, price,
            {"reduceOnly": True, "_is_contracts": True},
        )
        logger.info(
            "Position close order placed: %s %.6f @ %.2f",
            close_side, amount, price,
        )
        return order

    # ── Leverage ────────────────────────────────────────────

    def set_leverage(
        self, leverage: int, symbol: str = cfg.trading_symbol,
    ) -> None:
        """Set the leverage for a product.

        Uses ``POST /v2/products/{product_id}/orders/leverage``.
        """
        try:
            product_id = self._get_product_id(symbol)
            self._retry(
                self._request, "POST",
                f"/v2/products/{product_id}/orders/leverage",
                payload={"leverage": str(leverage)},
                auth=True,
            )
            logger.info("Leverage set to %dx for %s.", leverage, symbol)
        except Exception as exc:
            logger.warning(
                "set_leverage may not be supported or already set: %s", exc,
            )

    # ── Internal retry logic ────────────────────────────────

    def _retry(self, func, *args, **kwargs) -> Any:
        """Execute *func* with exponential-backoff retry on transient errors.

        Retries on connection errors, timeouts, and HTTP 5xx / 429.
        Does **not** retry on 4xx client errors or Delta API logic errors.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(1, cfg.max_retries + 1):
            try:
                return func(*args, **kwargs)
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
            ) as exc:
                last_exc = exc
                wait = 2 ** attempt
                logger.warning(
                    "API error (attempt %d/%d): %s — retrying in %ds",
                    attempt, cfg.max_retries, exc, wait,
                )
                time.sleep(wait)
            except requests.exceptions.HTTPError as exc:
                # Retry on 429 (rate limit) and 5xx (server errors)
                status = exc.response.status_code if exc.response is not None else 0
                if status == 429 or status >= 500:
                    last_exc = exc
                    wait = 2 ** attempt
                    logger.warning(
                        "HTTP %d (attempt %d/%d): %s — retrying in %ds",
                        status, attempt, cfg.max_retries, exc, wait,
                    )
                    time.sleep(wait)
                else:
                    # 4xx client errors — not retryable
                    logger.error("HTTP %d error (non-retryable): %s", status, exc)
                    raise
            except requests.exceptions.RequestException as exc:
                logger.error("Request error (non-retryable): %s", exc)
                raise
        logger.error("All %d retries exhausted.", cfg.max_retries)
        raise last_exc  # type: ignore[misc]
