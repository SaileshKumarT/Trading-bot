"""FastAPI dashboard server.

Provides REST endpoints for historical data and WebSocket endpoints
for real-time live data and log streaming. Serves the single-file
dashboard UI at the root path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from bot.config import cfg
from bot.database import Trade, Order, Signal, PnlSummary, get_session, init_db
from bot.dashboard.state import shared_state
from bot.dashboard.ws_manager import live_manager, logs_manager

logger = logging.getLogger(__name__)

# ── FastAPI app ─────────────────────────────────────────────

app = FastAPI(
    title="Delta Trading Bot Dashboard",
    description="Real-time trading dashboard for the Delta Exchange bot",
    version="1.0.0",
)

STATIC_DIR = Path(__file__).parent / "static"


# ── Serve the dashboard HTML ───────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard() -> HTMLResponse:
    """Serve the main dashboard HTML page.

    Returns:
        The complete single-file dashboard HTML.
    """
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


# ── REST Endpoints ─────────────────────────────────────────


@app.get("/api/status")
async def get_status() -> JSONResponse:
    """Return bot status, uptime, and kill switch state.

    Returns:
        JSON with bot_status, uptime_seconds, kill_switch, exchange, symbol.
    """
    snapshot = shared_state.get_live_snapshot()
    return JSONResponse({
        "bot_status": snapshot["bot_status"],
        "uptime_seconds": snapshot["uptime_seconds"],
        "kill_switch": os.getenv("KILL_SWITCH", "false").lower() in ("true", "1", "yes"),
        "exchange": "Delta Exchange India Testnet",
        "symbol": cfg.trading_symbol,
        "timeframe": cfg.timeframe,
        "leverage": cfg.leverage,
    })


@app.get("/api/account")
async def get_account() -> JSONResponse:
    """Return account balance, realized PnL, and daily PnL.

    Returns:
        JSON with balance, realized_pnl, unrealized_pnl, daily_pnl, capital.
    """
    snapshot = shared_state.get_live_snapshot()
    session = get_session()
    try:
        # Sum all realized PnL from closed trades
        closed_trades = session.query(Trade).filter(Trade.status == "closed").all()
        realized_pnl = sum(t.pnl or 0.0 for t in closed_trades)
        total_fees = sum(t.fees or 0.0 for t in closed_trades)
    finally:
        session.close()

    return JSONResponse({
        "capital": cfg.capital,
        "balance": snapshot["account"]["balance"],
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(snapshot["account"]["unrealized_pnl"], 2),
        "daily_pnl": round(snapshot["account"]["daily_pnl"], 2),
        "daily_loss_limit": round(cfg.capital * cfg.daily_loss_limit, 2),
        "net_pnl": round(realized_pnl + snapshot["account"]["unrealized_pnl"], 2),
        "net_pnl_pct": round(
            ((realized_pnl + snapshot["account"]["unrealized_pnl"]) / cfg.capital) * 100, 2
        ) if cfg.capital > 0 else 0.0,
        "total_fees": round(total_fees, 2),
    })


@app.get("/api/positions")
async def get_positions() -> JSONResponse:
    """Return all open positions with live PnL.

    Returns:
        JSON list of open position dicts.
    """
    snapshot = shared_state.get_live_snapshot()
    positions = snapshot.get("positions", [])

    # Also fetch from DB for enriched data
    session = get_session()
    try:
        open_trades = session.query(Trade).filter(Trade.status == "open").all()
        result = []
        for trade in open_trades:
            mid_price = snapshot["price"]["mid"]
            entry_px = trade.entry_price or 0
            if trade.side == "buy":
                unrealized = (mid_price - entry_px) * trade.qty if mid_price > 0 and entry_px > 0 else 0
            else:
                unrealized = (entry_px - mid_price) * trade.qty if mid_price > 0 and entry_px > 0 else 0

            unrealized_pct = (unrealized / (entry_px * trade.qty) * 100) if entry_px > 0 and trade.qty > 0 else 0

            # Calculate duration
            duration_str = ""
            if trade.entry_time:
                delta = datetime.now(timezone.utc) - trade.entry_time
                hours, remainder = divmod(int(delta.total_seconds()), 3600)
                minutes, _ = divmod(remainder, 60)
                duration_str = f"{hours}h {minutes}m"

            # Get SL/TP from orders
            sl_price = None
            tp_price = None
            orders = session.query(Order).filter(
                Order.trade_id == trade.id,
                Order.status == "open"
            ).all()
            for o in orders:
                if o.type == "stop_loss":
                    sl_price = o.price
                elif o.type == "take_profit":
                    tp_price = o.price

            result.append({
                "id": trade.id,
                "symbol": trade.symbol,
                "side": trade.side,
                "entry_price": entry_px,
                "current_price": mid_price,
                "qty": trade.qty,
                "leverage": trade.leverage,
                "unrealized_pnl": round(unrealized, 2),
                "unrealized_pnl_pct": round(unrealized_pct, 2),
                "stop_loss": sl_price,
                "take_profit": tp_price,
                "duration": duration_str,
                "status": trade.status,
            })
        return JSONResponse(result)
    finally:
        session.close()


@app.get("/api/trades")
async def get_trades(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
) -> JSONResponse:
    """Return paginated closed trade history.

    Args:
        page: Page number (1-indexed).
        per_page: Rows per page.

    Returns:
        JSON with trades list, total count, and pagination info.
    """
    session = get_session()
    try:
        total = session.query(Trade).filter(Trade.status == "closed").count()
        trades = (
            session.query(Trade)
            .filter(Trade.status == "closed")
            .order_by(Trade.exit_time.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )

        result = []
        for t in trades:
            duration_str = ""
            if t.entry_time and t.exit_time:
                delta = t.exit_time - t.entry_time
                hours, remainder = divmod(int(delta.total_seconds()), 3600)
                minutes, _ = divmod(remainder, 60)
                duration_str = f"{hours}h {minutes}m"

            pnl_pct = 0.0
            if t.entry_price and t.qty and t.entry_price > 0:
                pnl_pct = ((t.pnl or 0) / (t.entry_price * t.qty)) * 100

            result.append({
                "id": t.id,
                "entry_time": t.entry_time.isoformat() if t.entry_time else None,
                "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                "side": t.side,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "qty": t.qty,
                "pnl": round(t.pnl or 0, 2),
                "pnl_pct": round(pnl_pct, 2),
                "fees": round(t.fees or 0, 4),
                "exit_reason": t.exit_reason,
                "duration": duration_str,
            })

        return JSONResponse({
            "trades": result,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": math.ceil(total / per_page) if total > 0 else 1,
        })
    finally:
        session.close()


@app.get("/api/signals")
async def get_signals() -> JSONResponse:
    """Return the last 10 signals from the database.

    Returns:
        JSON list of signal dicts.
    """
    session = get_session()
    try:
        signals = (
            session.query(Signal)
            .order_by(Signal.timestamp.desc())
            .limit(10)
            .all()
        )
        result = []
        for s in signals:
            result.append({
                "id": s.id,
                "timestamp": s.timestamp.isoformat() if s.timestamp else None,
                "signal_type": s.signal_type,
                "ema_fast": round(s.ema_fast, 2),
                "ema_slow": round(s.ema_slow, 2),
                "rsi": round(s.rsi, 2),
                "price": round(s.price, 2),
            })
        return JSONResponse(result)
    finally:
        session.close()


@app.get("/api/stats")
async def get_stats() -> JSONResponse:
    """Return comprehensive trading statistics.

    Calculates win rate, profit factor, Sharpe ratio, max drawdown, etc.

    Returns:
        JSON with all computed statistics.
    """
    session = get_session()
    try:
        trades = (
            session.query(Trade)
            .filter(Trade.status == "closed")
            .order_by(Trade.exit_time.asc())
            .all()
        )

        total = len(trades)
        if total == 0:
            return JSONResponse({
                "total_trades": 0, "win_rate": 0, "avg_win": 0, "avg_loss": 0,
                "profit_factor": 0, "largest_win": 0, "largest_loss": 0,
                "avg_duration_minutes": 0, "total_fees": 0, "sharpe_ratio": 0,
                "max_drawdown_pct": 0,
            })

        pnls = [t.pnl or 0.0 for t in trades]
        fees = [t.fees or 0.0 for t in trades]

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        win_rate = (len(wins) / total) * 100 if total > 0 else 0
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        gross_wins = sum(wins)
        gross_losses = abs(sum(losses))
        profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")
        largest_win = max(pnls) if pnls else 0
        largest_loss = min(pnls) if pnls else 0
        total_fees = sum(fees)

        # Average duration
        durations = []
        for t in trades:
            if t.entry_time and t.exit_time:
                durations.append((t.exit_time - t.entry_time).total_seconds() / 60)
        avg_duration = sum(durations) / len(durations) if durations else 0

        # Sharpe ratio (annualized)
        if len(pnls) > 1:
            mean_pnl = sum(pnls) / len(pnls)
            variance = sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
            std_pnl = variance ** 0.5
            sharpe = (mean_pnl / std_pnl) * (252 ** 0.5) if std_pnl > 0 else 0
        else:
            sharpe = 0

        # Max drawdown
        cumulative = []
        running = 0.0
        for p in pnls:
            running += p
            cumulative.append(running)

        peak = cumulative[0]
        max_dd = 0.0
        for c in cumulative:
            if c > peak:
                peak = c
            dd = peak - c
            if dd > max_dd:
                max_dd = dd
        max_dd_pct = (max_dd / cfg.capital) * 100 if cfg.capital > 0 else 0

        return JSONResponse({
            "total_trades": total,
            "win_rate": round(win_rate, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "∞",
            "largest_win": round(largest_win, 2),
            "largest_loss": round(largest_loss, 2),
            "avg_duration_minutes": round(avg_duration, 1),
            "total_fees": round(total_fees, 2),
            "sharpe_ratio": round(sharpe, 2),
            "max_drawdown_pct": round(max_dd_pct, 2),
        })
    finally:
        session.close()


@app.get("/api/candles")
async def get_candles() -> JSONResponse:
    """Return candle data with EMA values for the price chart.

    Returns:
        JSON with candles array and EMA series.
    """
    data = shared_state.get_candle_snapshot()
    return JSONResponse(data)


@app.post("/api/kill")
async def activate_kill_switch(
    x_kill_confirm: Optional[str] = Header(None),
) -> JSONResponse:
    """Activate the kill switch.

    Requires the header X-Kill-Confirm: yes for safety.

    Args:
        x_kill_confirm: Confirmation header value.

    Returns:
        JSON confirmation or error.
    """
    if x_kill_confirm != "yes":
        raise HTTPException(
            status_code=400,
            detail="Missing or incorrect X-Kill-Confirm header. Send 'X-Kill-Confirm: yes'.",
        )

    os.environ["KILL_SWITCH"] = "true"
    shared_state.set_bot_stopped()
    logger.critical("Kill switch activated via dashboard API.")
    return JSONResponse({"status": "kill_switch_activated", "message": "Kill switch has been activated."})


@app.get("/api/pnl_chart")
async def get_pnl_chart() -> JSONResponse:
    """Return cumulative PnL data points for the performance chart.

    Returns:
        JSON with timestamps and cumulative PnL values.
    """
    session = get_session()
    try:
        trades = (
            session.query(Trade)
            .filter(Trade.status == "closed")
            .order_by(Trade.exit_time.asc())
            .all()
        )

        points = []
        cumulative = 0.0
        for t in trades:
            cumulative += (t.pnl or 0.0) - (t.fees or 0.0)
            points.append({
                "timestamp": t.exit_time.isoformat() if t.exit_time else None,
                "trade_pnl": round(t.pnl or 0, 2),
                "cumulative_pnl": round(cumulative, 2),
                "trade_number": t.id,
            })

        return JSONResponse(points)
    finally:
        session.close()


# ── WebSocket: Live data ───────────────────────────────────


@app.websocket("/ws/live")
async def websocket_live(websocket: WebSocket) -> None:
    """WebSocket endpoint that pushes live state every 2 seconds.

    Args:
        websocket: The incoming WebSocket connection.
    """
    await live_manager.connect(websocket)
    try:
        while True:
            snapshot = shared_state.get_live_snapshot()
            await websocket.send_json(snapshot)
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        live_manager.disconnect(websocket)
    except Exception:
        live_manager.disconnect(websocket)


# ── WebSocket: Log stream ─────────────────────────────────


@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket) -> None:
    """WebSocket endpoint that streams new log lines in real time.

    Sends the last 50 log lines on connect, then pushes new lines
    as they arrive (checked every 1 second).

    Args:
        websocket: The incoming WebSocket connection.
    """
    await logs_manager.connect(websocket)
    last_count = 0
    try:
        # Send initial batch
        logs = shared_state.get_recent_logs(50)
        await websocket.send_json({"type": "initial", "logs": logs})
        last_count = len(logs)

        while True:
            await asyncio.sleep(1)
            current_logs = shared_state.get_recent_logs(50)
            if len(current_logs) > last_count:
                new_logs = current_logs[last_count:]
                for log_entry in new_logs:
                    await websocket.send_json({"type": "new", "log": log_entry})
                last_count = len(current_logs)
            elif len(current_logs) < last_count:
                # Buffer wrapped around
                last_count = len(current_logs)
    except WebSocketDisconnect:
        logs_manager.disconnect(websocket)
    except Exception:
        logs_manager.disconnect(websocket)


# ── Dashboard log handler (captures bot logs into shared state) ─


class DashboardLogHandler(logging.Handler):
    """Custom logging handler that feeds log lines into the shared state buffer.

    Attach this to the root logger so all bot logs appear in the dashboard.
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Process a log record and add it to shared state.

        Args:
            record: The log record to process.
        """
        try:
            msg = self.format(record)
            shared_state.add_log_line(record.levelname, msg)
        except Exception:
            pass


# ── Server runner ──────────────────────────────────────────


def start_dashboard_server(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Start the FastAPI dashboard server in the current thread.

    Args:
        host: Bind address.
        port: Bind port.
    """
    logger.info("Starting dashboard server on %s:%d", host, port)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )


def start_dashboard_in_thread(port: int = 8080) -> threading.Thread:
    """Start the dashboard server in a daemon background thread.

    Args:
        port: Port to bind the dashboard to.

    Returns:
        The started daemon thread.
    """
    thread = threading.Thread(
        target=start_dashboard_server,
        kwargs={"host": "0.0.0.0", "port": port},
        daemon=True,
        name="dashboard-server",
    )
    thread.start()
    logger.info("Dashboard server thread started on port %d.", port)
    return thread
