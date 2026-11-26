"""Order execution engine.

Handles the full lifecycle of a trade:
  1. Receives a signal from the strategy.
  2. Checks risk limits.
  3. Places a limit entry order at mid-price.
  4. Tracks fill status on subsequent candle ticks.
  5. On fill, places SL and TP orders.
  6. Monitors SL/TP fills and updates trade records.
  7. Cancels stale unfilled entries after ORDER_EXPIRY_CANDLES.
  8. Cleans up orphaned orders on startup and periodically.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Set

from bot.config import cfg
from bot.database import Order, Signal, Trade, get_session
from bot.exchange import Exchange
from bot.risk import RiskManager
from bot.pnl import calculate_trade_pnl, calculate_fees
from bot.strategy import StrategySignal
from bot import telegram_alerts

logger = logging.getLogger(__name__)


class Executor:
    """Manages order placement, fill tracking, and trade lifecycle."""

    def __init__(self, exchange: Exchange, risk_manager: RiskManager) -> None:
        """Initialise the executor.

        Args:
            exchange: Configured Exchange wrapper instance.
            risk_manager: RiskManager instance for sizing and limits.
        """
        self._exchange = exchange
        self._risk = risk_manager
        # Track how many candles each pending entry has been waiting
        self._pending_entry_candle_counts: Dict[int, int] = {}

    # ── Signal processing ───────────────────────────────────

    def process_signal(self, signal: StrategySignal) -> None:
        """Process a trading signal: validate, size, and submit an entry order.

        Args:
            signal: The StrategySignal from the strategy module.
        """
        session = get_session()
        try:
            # Log signal to DB
            db_signal = Signal(
                symbol=cfg.trading_symbol,
                timeframe=cfg.timeframe,
                signal_type=signal.signal_type,
                ema_fast=signal.ema_fast,
                ema_slow=signal.ema_slow,
                rsi=signal.rsi,
                price=signal.price,
            )
            session.add(db_signal)
            session.commit()

            # Telegram alert: signal detected
            telegram_alerts.alert_signal_detected(
                symbol=cfg.trading_symbol,
                direction=signal.signal_type,
                price=signal.price,
                ema_fast=signal.ema_fast,
                ema_slow=signal.ema_slow,
                rsi=signal.rsi,
            )

            # Risk checks
            if not self._risk.can_open_new_position():
                logger.info("Risk check failed — skipping signal.")
                return

            # Spread filter — skip if bid-ask spread is too wide
            bid_ask = self._exchange.get_bid_ask(cfg.trading_symbol)
            best_bid = bid_ask["bid"]
            best_ask = bid_ask["ask"]
            mid_price = bid_ask["mid"]

            if mid_price <= 0:
                logger.error("Invalid mid-price: %.2f — aborting entry.", mid_price)
                return

            if best_bid > 0 and best_ask > 0:
                spread_pct = (best_ask - best_bid) / mid_price
                if spread_pct > cfg.spread_max_pct:
                    logger.warning(
                        "Spread too wide: %.4f%% (max %.4f%%) — skipping signal. bid=%.2f ask=%.2f",
                        spread_pct * 100, cfg.spread_max_pct * 100, best_bid, best_ask,
                    )
                    return
                logger.info("Spread check passed: %.4f%% (max %.4f%%)", spread_pct * 100, cfg.spread_max_pct * 100)

            # Calculate position size
            qty = self._risk.calculate_position_size(mid_price)
            if qty <= 0:
                logger.error("Invalid position size: %.6f — aborting.", qty)
                return

            # Calculate SL/TP levels
            sl_price = self._risk.calculate_stop_loss(mid_price, signal.signal_type)
            tp_price = self._risk.calculate_take_profit(mid_price, signal.signal_type)
            entry_fee = self._risk.estimate_fees(qty, mid_price)

            logger.info(
                "Preparing entry: %s %.6f @ %.2f | SL=%.2f | TP=%.2f | Fee≈%.4f",
                signal.signal_type.upper(),
                qty,
                mid_price,
                sl_price,
                tp_price,
                entry_fee,
            )

            # Create trade record
            trade = Trade(
                symbol=cfg.trading_symbol,
                side=signal.signal_type,
                qty=qty,
                leverage=cfg.leverage,
                status="pending",
                fees=0.0,
            )
            session.add(trade)
            session.commit()

            # Place limit entry order
            try:
                order_resp = self._exchange.place_limit_order(
                    symbol=cfg.trading_symbol,
                    side=signal.signal_type,
                    amount=qty,
                    price=mid_price,
                )
            except Exception as exc:
                logger.error("Failed to place entry order: %s", exc)
                trade.status = "failed"
                session.commit()
                telegram_alerts.alert_error(f"Entry order failed: {exc}")
                return

            # Record order in DB
            order_record = Order(
                trade_id=trade.id,
                exchange_order_id=str(order_resp.get("id", "")),
                type="limit",
                side=signal.signal_type,
                price=mid_price,
                qty=qty,
                status="open",
            )
            session.add(order_record)
            session.commit()

            # Track candle count for expiry
            self._pending_entry_candle_counts[trade.id] = 0

            telegram_alerts.alert_order_placed(
                order_type="limit",
                side=signal.signal_type,
                qty=qty,
                price=mid_price,
            )

            logger.info(
                "Entry order placed: trade_id=%d, exchange_order_id=%s",
                trade.id,
                order_record.exchange_order_id,
            )

        except Exception as exc:
            logger.error("Error processing signal: %s", exc, exc_info=True)
            session.rollback()
            telegram_alerts.alert_error(f"Signal processing error: {exc}")
        finally:
            session.close()

    # ── Order status management ─────────────────────────────

    def check_pending_entries(self) -> None:
        """Check all pending entry orders for fills or expiry.

        Called on each candle tick. Increments the wait counter for each
        pending entry and cancels any that exceed ORDER_EXPIRY_CANDLES.
        """
        session = get_session()
        try:
            pending_trades = (
                session.query(Trade)
                .filter(Trade.status == "pending")
                .all()
            )

            for trade in pending_trades:
                entry_orders = (
                    session.query(Order)
                    .filter(
                        Order.trade_id == trade.id,
                        Order.type == "limit",
                        Order.status == "open",
                    )
                    .all()
                )

                for order in entry_orders:
                    if not order.exchange_order_id:
                        continue

                    # Check order status on exchange
                    try:
                        exchange_order = self._exchange.fetch_order(
                            order.exchange_order_id, cfg.trading_symbol
                        )
                    except Exception as exc:
                        logger.error(
                            "Error fetching order %s: %s",
                            order.exchange_order_id,
                            exc,
                        )
                        continue

                    status = exchange_order.get("status", "")

                    if status == "closed":
                        # Order filled!
                        self._handle_entry_fill(session, trade, order, exchange_order)
                    elif status == "canceled" or status == "cancelled":
                        order.status = "cancelled"
                        trade.status = "cancelled"
                        session.commit()
                        logger.info("Entry order %s was cancelled on exchange.", order.exchange_order_id)
                    else:
                        # Still open — increment candle counter
                        candle_count = self._pending_entry_candle_counts.get(trade.id, 0) + 1
                        self._pending_entry_candle_counts[trade.id] = candle_count
                        logger.debug(
                            "Entry order %s still pending (%d/%d candles).",
                            order.exchange_order_id,
                            candle_count,
                            cfg.order_expiry_candles,
                        )

                        # Cancel if expired
                        if candle_count >= cfg.order_expiry_candles:
                            self._cancel_entry(session, trade, order, "expired after 3 candles")

        except Exception as exc:
            logger.error("Error checking pending entries: %s", exc, exc_info=True)
            session.rollback()
        finally:
            session.close()

    def _handle_entry_fill(
        self,
        session: Any,
        trade: Trade,
        order: Order,
        exchange_order: Dict[str, Any],
    ) -> None:
        """Handle a filled entry order — update DB and place SL/TP.

        Args:
            session: Active DB session.
            trade: Trade ORM object.
            order: Order ORM object.
            exchange_order: Exchange response dict.
        """
        fill_price = exchange_order.get("average") or exchange_order.get("price") or order.price

        # The exchange reports 'filled' in contracts (e.g. 1 contract),
        # not in base currency (BTC).  Convert back to BTC for the trade
        # record and fee calculations, but keep the contract count for
        # SL/TP orders so they aren't double-converted.
        filled_contracts = exchange_order.get("filled") or 0
        contract_value = self._exchange._get_contract_value(cfg.trading_symbol)

        if filled_contracts > 0:
            filled_qty = filled_contracts * contract_value  # contracts → BTC
        else:
            # Fallback: order.qty is already in BTC
            filled_qty = order.qty
            filled_contracts = max(1, round(filled_qty / contract_value))

        order.status = "filled"
        trade.status = "open"
        trade.entry_price = fill_price
        trade.entry_time = datetime.now(timezone.utc)
        trade.qty = filled_qty  # Update to actual filled amount in BTC
        trade.fees = calculate_fees(filled_qty, fill_price)

        # Remove from pending tracker
        self._pending_entry_candle_counts.pop(trade.id, None)

        session.commit()

        logger.info(
            "ENTRY FILLED: trade_id=%d, %s %.6f BTC (%d contracts) @ %.2f",
            trade.id,
            trade.side.upper(),
            filled_qty,
            filled_contracts,
            fill_price,
        )

        telegram_alerts.alert_order_filled(
            side=trade.side, qty=filled_qty, price=fill_price
        )

        # Place stop-loss and take-profit orders using the contract count
        self._place_exit_orders(session, trade, fill_price, int(filled_contracts))

    def _place_exit_orders(
        self,
        session: Any,
        trade: Trade,
        entry_price: float,
        exit_contracts: int,
    ) -> None:
        """Place stop-loss and take-profit stop orders after entry fill.

        Both orders use Delta's stop_order_type so they only trigger when
        the market reaches the stop_price, instead of filling immediately
        like regular limit orders would.

        Verifies that a matching position exists on the exchange before
        placing any exit orders.  If no position is found, the trade is
        marked as failed.

        Stores the exchange order IDs on the Trade record for fast lookup.

        Args:
            session: Active DB session.
            trade: The filled trade.
            entry_price: Entry fill price.
            exit_contracts: Number of contracts to close (must match entry fill).
        """
        # ── Position verification ──────────────────────────────
        try:
            positions = self._exchange.fetch_positions([cfg.trading_symbol])
        except Exception as exc:
            logger.error("Failed to fetch positions for verification: %s", exc)
            positions = []

        expected_side = "long" if trade.side == "buy" else "short"
        matching_position = None

        logger.info(
            "Position verification for trade #%d: looking for side=%s, "
            "got %d position(s) from exchange: %s",
            trade.id, expected_side, len(positions),
            [(p.get("side"), p.get("contracts"), p.get("symbol")) for p in positions],
        )

        for pos in positions:
            pos_contracts = float(pos.get("contracts", 0) or 0)
            pos_side = pos.get("side", "")
            if pos_side == expected_side and pos_contracts > 0:
                matching_position = pos
                break

        if matching_position is None:
            logger.error(
                "NO MATCHING POSITION on exchange for trade #%d (%s %s). "
                "Expected side=%s. Marking trade as failed — will NOT place SL/TP.",
                trade.id, trade.side, cfg.trading_symbol, expected_side,
            )
            trade.status = "failed"
            trade.exit_reason = "no_position_after_fill"
            trade.exit_time = datetime.now(timezone.utc)
            session.commit()
            telegram_alerts.alert_error(
                f"Trade #{trade.id} filled but no position found on exchange. Marked as failed."
            )
            return

        logger.info(
            "Position verified for trade #%d: side=%s, contracts=%.0f",
            trade.id, matching_position["side"], matching_position["contracts"],
        )

        sl_price = self._risk.calculate_stop_loss(entry_price, trade.side)
        tp_price = self._risk.calculate_take_profit(entry_price, trade.side)
        exit_side = "sell" if trade.side == "buy" else "buy"

        # Place stop-loss as a stop-limit order
        try:
            sl_order = self._exchange.place_stop_order(
                symbol=cfg.trading_symbol,
                side=exit_side,
                amount=exit_contracts,
                stop_price=sl_price,
                stop_order_type="stop_loss_order",
                limit_price=sl_price,
                params={"_is_contracts": True},
            )
            sl_exchange_id = str(sl_order.get("id", ""))
            sl_record = Order(
                trade_id=trade.id,
                exchange_order_id=sl_exchange_id,
                type="stop_loss",
                side=exit_side,
                price=sl_price,
                qty=trade.qty,
                status="open",
            )
            session.add(sl_record)
            # Store SL order ID directly on trade for fast lookup
            trade.sl_order_id = sl_exchange_id
            logger.info("Stop-loss placed: %s %d contracts, stop @ %.2f | id=%s", exit_side, exit_contracts, sl_price, sl_exchange_id)
            telegram_alerts.alert_order_placed("stop_loss", exit_side, trade.qty, sl_price)
        except Exception as exc:
            logger.error("Failed to place stop-loss: %s", exc)
            telegram_alerts.alert_error(f"Stop-loss placement failed: {exc}")

        # Place take-profit as a stop-limit order.
        # If the position was already closed (e.g. SL filled instantly),
        # Delta returns "no_position_for_reduce_only" — handle gracefully.
        try:
            tp_order = self._exchange.place_stop_order(
                symbol=cfg.trading_symbol,
                side=exit_side,
                amount=exit_contracts,
                stop_price=tp_price,
                stop_order_type="take_profit_order",
                limit_price=tp_price,
                params={"_is_contracts": True},
            )
            tp_exchange_id = str(tp_order.get("id", ""))
            tp_record = Order(
                trade_id=trade.id,
                exchange_order_id=tp_exchange_id,
                type="take_profit",
                side=exit_side,
                price=tp_price,
                qty=trade.qty,
                status="open",
            )
            session.add(tp_record)
            # Store TP order ID directly on trade for fast lookup
            trade.tp_order_id = tp_exchange_id
            logger.info("Take-profit placed: %s %d contracts, stop @ %.2f | id=%s", exit_side, exit_contracts, tp_price, tp_exchange_id)
            telegram_alerts.alert_order_placed("take_profit", exit_side, trade.qty, tp_price)
        except RuntimeError as exc:
            # Delta API error — position may already be closed by the SL
            err_msg = str(exc).lower()
            if "no_position" in err_msg or "reduce_only" in err_msg:
                logger.warning(
                    "TP skipped for trade_id=%d: position already closed (%s).",
                    trade.id, exc,
                )
            else:
                logger.error("Failed to place take-profit: %s", exc)
                telegram_alerts.alert_error(f"Take-profit placement failed: {exc}")
        except Exception as exc:
            logger.error("Failed to place take-profit: %s", exc)
            telegram_alerts.alert_error(f"Take-profit placement failed: {exc}")

        session.commit()

    def _cancel_entry(
        self, session: Any, trade: Trade, order: Order, reason: str
    ) -> None:
        """Cancel a pending entry order and update DB records.

        Args:
            session: Active DB session.
            trade: The trade with the pending entry.
            order: The entry Order to cancel.
            reason: Human-readable cancellation reason.
        """
        try:
            self._exchange.cancel_order(order.exchange_order_id, cfg.trading_symbol)
        except Exception as exc:
            logger.warning("Could not cancel order %s on exchange: %s", order.exchange_order_id, exc)

        order.status = "cancelled"
        trade.status = "cancelled"
        self._pending_entry_candle_counts.pop(trade.id, None)
        session.commit()

        logger.info(
            "Entry cancelled: trade_id=%d, order=%s, reason=%s",
            trade.id,
            order.exchange_order_id,
            reason,
        )
        telegram_alerts.alert_order_cancelled(order.exchange_order_id, reason)

    # ── Cancel all orders for a trade ──────────────────────

    def _cancel_all_trade_orders(
        self, session: Any, trade: Trade, reason: str = "trade_closed",
    ) -> int:
        """Cancel every open order belonging to a trade on both exchange and DB.

        Uses individual cancels first for precision, then falls back to
        cancel_all_orders if any individual cancel fails — a belt-and-braces
        approach that prevents orphaned orders.

        Args:
            session: Active DB session.
            trade: The trade whose orders should be cancelled.
            reason: Reason for cancellation (for logging).

        Returns:
            Number of orders cancelled.
        """
        open_orders = (
            session.query(Order)
            .filter(
                Order.trade_id == trade.id,
                Order.status == "open",
            )
            .all()
        )

        if not open_orders:
            return 0

        cancelled = 0
        failed_ids: List[str] = []

        for order in open_orders:
            if not order.exchange_order_id:
                order.status = "cancelled"
                trade.clear_exit_order_id(order.type)
                cancelled += 1
                continue

            try:
                self._exchange.cancel_order(
                    order.exchange_order_id, cfg.trading_symbol
                )
                order.status = "cancelled"
                trade.clear_exit_order_id(order.type)
                cancelled += 1
                logger.info(
                    "Cancelled %s order %s for trade #%d (%s).",
                    order.type, order.exchange_order_id, trade.id, reason,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to cancel %s order %s for trade #%d: %s",
                    order.type, order.exchange_order_id, trade.id, exc,
                )
                failed_ids.append(order.exchange_order_id)

        # If any individual cancel failed, nuke all orders for the symbol
        # as a safety net — this guarantees nothing is left behind.
        if failed_ids:
            logger.warning(
                "Retrying with cancel_all_orders for trade #%d (failed: %s).",
                trade.id, failed_ids,
            )
            try:
                self._exchange.cancel_all_orders(cfg.trading_symbol)
                # Mark all remaining as cancelled in DB
                for order in open_orders:
                    if order.status == "open":
                        order.status = "cancelled"
                        trade.clear_exit_order_id(order.type)
                        cancelled += 1
                logger.info(
                    "cancel_all_orders fallback succeeded for trade #%d.", trade.id,
                )
            except Exception as exc:
                logger.error(
                    "cancel_all_orders fallback ALSO failed for trade #%d: %s",
                    trade.id, exc,
                )

        session.commit()
        return cancelled

    # ── Exit monitoring ─────────────────────────────────────

    def check_open_trades(self) -> None:
        """Check SL/TP orders for all open trades and handle fills.

        Called on each candle tick. When an SL or TP fills, closes the
        trade, calculates PnL, and cancels ALL remaining orders for that
        trade.
        """
        session = get_session()
        try:
            open_trades = (
                session.query(Trade)
                .filter(Trade.status == "open")
                .all()
            )

            for trade in open_trades:
                exit_orders = (
                    session.query(Order)
                    .filter(
                        Order.trade_id == trade.id,
                        Order.type.in_(["stop_loss", "take_profit"]),
                        Order.status == "open",
                    )
                    .all()
                )

                for order in exit_orders:
                    if not order.exchange_order_id:
                        continue

                    try:
                        exchange_order = self._exchange.fetch_order(
                            order.exchange_order_id, cfg.trading_symbol
                        )
                    except Exception as exc:
                        logger.error(
                            "Error fetching exit order %s: %s",
                            order.exchange_order_id,
                            exc,
                        )
                        continue

                    status = exchange_order.get("status", "")

                    if status == "closed":
                        # Exit order filled
                        fill_price = (
                            exchange_order.get("average")
                            or exchange_order.get("price")
                            or order.price
                        )
                        self._handle_exit_fill(
                            session, trade, order, fill_price
                        )
                        break  # Only one exit should fill per trade

        except Exception as exc:
            logger.error("Error checking open trades: %s", exc, exc_info=True)
            session.rollback()
        finally:
            session.close()

    def _handle_exit_fill(
        self,
        session: Any,
        trade: Trade,
        filled_order: Order,
        exit_price: float,
    ) -> None:
        """Handle a filled exit (SL or TP) order.

        Updates trade record, calculates PnL, cancels the counterpart
        exit order (SL↔TP) using stored order IDs, and sends alerts.

        Args:
            session: Active DB session.
            trade: The trade being exited.
            filled_order: The exit order that was filled.
            exit_price: The fill price.
        """
        filled_order.status = "filled"

        # Calculate PnL
        pnl = calculate_trade_pnl(
            side=trade.side,
            entry_price=trade.entry_price or 0,
            exit_price=exit_price,
            qty=trade.qty,
            leverage=trade.leverage,
        )
        exit_fee = calculate_fees(trade.qty, exit_price)
        total_fees = (trade.fees or 0.0) + exit_fee

        # ── Cancel the counterpart order (SL↔TP) BEFORE committing ─
        # This ensures that if the bot crashes after commit, there are
        # no orphaned counterpart orders left on the exchange.
        if filled_order.type == "stop_loss":
            counterpart_id = trade.tp_order_id
            counterpart_label = "take_profit"
        else:
            counterpart_id = trade.sl_order_id
            counterpart_label = "stop_loss"

        counterpart_cancel_ok = False
        if counterpart_id:
            try:
                self._exchange.cancel_order(counterpart_id, cfg.trading_symbol)
                counterpart_cancel_ok = True
                logger.info(
                    "Cancelled counterpart %s order %s after %s fill for trade #%d.",
                    counterpart_label, counterpart_id, filled_order.type, trade.id,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to cancel counterpart %s order %s for trade #%d: %s. "
                    "Falling back to cancel_all_trade_orders.",
                    counterpart_label, counterpart_id, trade.id, exc,
                )
                # Fallback: cancel all remaining orders for this trade
                self._cancel_all_trade_orders(
                    session, trade, reason=f"{filled_order.type}_filled_fallback"
                )
                counterpart_cancel_ok = True  # fallback attempted

            # Mark the counterpart order as cancelled in DB
            counterpart_db = (
                session.query(Order)
                .filter(
                    Order.exchange_order_id == counterpart_id,
                    Order.status == "open",
                )
                .first()
            )
            if counterpart_db:
                counterpart_db.status = "cancelled"
        else:
            # No stored counterpart ID — fall back to cancelling all
            num_cancelled = self._cancel_all_trade_orders(
                session, trade, reason=f"{filled_order.type}_filled"
            )
            counterpart_cancel_ok = True
            if num_cancelled > 0:
                logger.info(
                    "Cancelled %d remaining order(s) after %s fill for trade #%d.",
                    num_cancelled, filled_order.type, trade.id,
                )

        # ── Now commit trade closure + counterpart DB update atomically ─
        trade.exit_price = exit_price
        trade.exit_time = datetime.now(timezone.utc)
        trade.pnl = pnl
        trade.fees = total_fees
        trade.status = "closed"
        trade.exit_reason = filled_order.type  # "stop_loss" or "take_profit"

        session.commit()

        # Update daily PnL
        self._risk.update_daily_pnl(pnl)

        logger.info(
            "TRADE CLOSED: id=%d, exit_reason=%s, PnL=%.2f, fees=%.4f",
            trade.id,
            filled_order.type,
            pnl,
            total_fees,
        )

        # Telegram alerts
        if filled_order.type == "stop_loss":
            telegram_alerts.alert_stop_loss_triggered(
                cfg.trading_symbol, exit_price, pnl
            )
        else:
            telegram_alerts.alert_take_profit_triggered(
                cfg.trading_symbol, exit_price, pnl
            )

        # Check daily loss limit
        if self._risk.is_daily_limit_breached():
            limit_val = -(cfg.capital * cfg.daily_loss_limit)
            telegram_alerts.alert_daily_loss_limit(self._risk.daily_pnl, limit_val)

    # ── Orphaned order cleanup ─────────────────────────────

    def _try_record_liquidation_pnl(self, trade: Trade) -> bool:
        """Attempt to fetch liquidation/settlement info and record PnL.

        Queries the exchange for the position's recent state to find
        a liquidation or settlement price.  If found, populates
        ``exit_price``, ``pnl``, ``exit_reason``, and ``exit_time``.

        Args:
            trade: The orphaned trade to inspect.

        Returns:
            ``True`` if liquidation info was found and applied.
        """
        try:
            product_id = self._exchange._get_product_id(cfg.trading_symbol)
            # Fetch closed/liquidated position info from Delta API
            raw = self._exchange._retry(
                self._exchange._request, "GET", "/v2/positions",
                params={"product_id": str(product_id)},
                auth=True,
            )

            positions = [raw] if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])

            for p in positions:
                if not isinstance(p, dict):
                    continue
                # Check for liquidation markers
                state = str(p.get("state", "")).lower()
                is_liquidated = "liquidat" in state or float(p.get("liquidation_price", 0)) > 0

                if is_liquidated or float(p.get("size", 0)) == 0:
                    # Try to get a settlement / bankruptcy price
                    bankruptcy_price = float(p.get("bankruptcy_price", 0))
                    liquidation_price = float(p.get("liquidation_price", 0))
                    exit_price = bankruptcy_price or liquidation_price

                    if exit_price > 0 and trade.entry_price:
                        trade.exit_price = exit_price
                        trade.pnl = calculate_trade_pnl(
                            side=trade.side,
                            entry_price=trade.entry_price,
                            exit_price=exit_price,
                            qty=trade.qty,
                            leverage=trade.leverage,
                        )
                        exit_fee = calculate_fees(trade.qty, exit_price)
                        trade.fees = (trade.fees or 0.0) + exit_fee
                        trade.exit_reason = "liquidation"
                        trade.exit_time = datetime.now(timezone.utc)
                        logger.warning(
                            "Liquidation detected for trade #%d: exit_price=%.2f, "
                            "PnL=%.2f, fees=%.4f",
                            trade.id, exit_price, trade.pnl, trade.fees,
                        )
                        self._risk.update_daily_pnl(trade.pnl)
                        telegram_alerts.alert_error(
                            f"Trade #{trade.id} liquidated @ {exit_price:.2f}, "
                            f"PnL: {trade.pnl:.2f}"
                        )
                        return True

            # Also check recent order fills for this product as a fallback
            try:
                raw_orders = self._exchange._retry(
                    self._exchange._request, "GET", "/v2/orders",
                    params={
                        "product_id": str(product_id),
                        "state": "closed",
                        "page_size": "5",
                    },
                    auth=True,
                )
                recent_orders = raw_orders if isinstance(raw_orders, list) else []
                for ro in recent_orders:
                    if not isinstance(ro, dict):
                        continue
                    # Look for reduce-only fills that closed the position
                    if ro.get("reduce_only") and float(ro.get("size", 0)) > 0:
                        avg_fill = float(ro.get("average_fill_price", 0))
                        if avg_fill > 0 and trade.entry_price:
                            trade.exit_price = avg_fill
                            trade.pnl = calculate_trade_pnl(
                                side=trade.side,
                                entry_price=trade.entry_price,
                                exit_price=avg_fill,
                                qty=trade.qty,
                                leverage=trade.leverage,
                            )
                            exit_fee = calculate_fees(trade.qty, avg_fill)
                            trade.fees = (trade.fees or 0.0) + exit_fee
                            trade.exit_reason = "liquidation"
                            trade.exit_time = datetime.now(timezone.utc)
                            logger.warning(
                                "Liquidation fill found for trade #%d: exit_price=%.2f, "
                                "PnL=%.2f",
                                trade.id, avg_fill, trade.pnl,
                            )
                            self._risk.update_daily_pnl(trade.pnl)
                            telegram_alerts.alert_error(
                                f"Trade #{trade.id} liquidated @ {avg_fill:.2f}, "
                                f"PnL: {trade.pnl:.2f}"
                            )
                            return True
            except Exception as exc:
                logger.debug("Could not fetch order history for liquidation check: %s", exc)

        except Exception as exc:
            logger.debug("Could not fetch position info for liquidation check: %s", exc)

        return False

    def cleanup_orphaned_orders(self) -> int:
        """Find and cancel orphaned orders and sync stale DB trades.

        Logic is split into mutually exclusive branches to avoid
        double-cancellation:

        - **No open position:** cancel ALL exchange orders (they are all
          orphaned) and mark stale DB trades as failed, attempting to
          record liquidation PnL when possible.
        - **Position exists:** cancel only exchange orders not linked to
          an active (open/pending) trade in the DB.

        In both cases, stale ``sl_order_id`` / ``tp_order_id`` references
        on Trade records are cleared when their orders are cancelled.

        Returns:
            Number of orphaned orders cancelled.
        """
        # ── Fetch exchange state ───────────────────────────────
        try:
            exchange_orders = self._exchange.fetch_open_orders(cfg.trading_symbol)
        except Exception as exc:
            logger.error("Failed to fetch open orders for orphan cleanup: %s", exc)
            return 0

        try:
            exchange_positions = self._exchange.fetch_positions([cfg.trading_symbol])
        except Exception as exc:
            logger.error("Failed to fetch positions for orphan cleanup: %s", exc)
            exchange_positions = []

        has_open_position = any(
            float(p.get("contracts", 0) or 0) > 0 for p in exchange_positions
        )

        # ── Always log exchange state for visibility ──────────
        pos_details = [
            f"{p.get('side')} {p.get('contracts')} contracts"
            for p in exchange_positions
            if float(p.get("contracts", 0) or 0) > 0
        ]
        logger.info(
            "Orphan check: %d open position(s), %d open order(s) on exchange.%s",
            len(exchange_positions) if has_open_position else 0,
            len(exchange_orders),
            f" Positions: {pos_details}" if pos_details else "",
        )

        exchange_order_ids: Set[str] = {
            str(o.get("id", "")) for o in exchange_orders if o.get("id")
        }

        session = get_session()
        try:
            cancelled = 0

            if not has_open_position:
                # ── No position: cancel ALL exchange orders (superset) ─
                if exchange_order_ids:
                    logger.warning(
                        "No open position on exchange but %d open order(s) found — "
                        "cancelling ALL as orphaned.",
                        len(exchange_order_ids),
                    )
                    for order_id in exchange_order_ids:
                        try:
                            self._exchange.cancel_order(order_id, cfg.trading_symbol)
                            cancelled += 1
                            logger.info(
                                "Cancelled orphaned order %s (no matching position).",
                                order_id,
                            )
                        except Exception as exc:
                            logger.warning(
                                "Failed to cancel order %s (no position): %s",
                                order_id, exc,
                            )

                        # Update DB order record and clear stale Trade references
                        db_order = (
                            session.query(Order)
                            .filter(Order.exchange_order_id == order_id)
                            .first()
                        )
                        if db_order and db_order.status == "open":
                            db_order.status = "cancelled"
                            if db_order.trade_id:
                                linked_trade = (
                                    session.query(Trade)
                                    .filter(Trade.id == db_order.trade_id)
                                    .first()
                                )
                                if linked_trade:
                                    linked_trade.clear_exit_order_id(db_order.type)
                                    logger.info(
                                        "Cleared stale %s ID on trade #%d (order %s cancelled).",
                                        db_order.type, linked_trade.id, order_id,
                                    )

                # ── Stale DB trades with no exchange position ──────────
                stale_open_trades = (
                    session.query(Trade)
                    .filter(Trade.status == "open")
                    .all()
                )
                for trade in stale_open_trades:
                    # Attempt to record liquidation PnL before marking failed
                    liquidation_found = self._try_record_liquidation_pnl(trade)

                    if not liquidation_found:
                        trade.status = "failed"
                        trade.exit_time = datetime.now(timezone.utc)
                        trade.exit_reason = "orphan_no_position_unknown"
                        logger.warning(
                            "DB trade #%d is 'open' but no exchange position exists. "
                            "Marking as failed (orphan_no_position_unknown).",
                            trade.id,
                        )
                    else:
                        trade.status = "closed"

                    # Clear stale exit order references
                    trade.clear_exit_order_id("stop_loss")
                    trade.clear_exit_order_id("take_profit")

                    # Cancel any remaining DB orders for this trade
                    remaining_orders = (
                        session.query(Order)
                        .filter(Order.trade_id == trade.id, Order.status == "open")
                        .all()
                    )
                    for o in remaining_orders:
                        o.status = "cancelled"

            else:
                # ── Position exists: cancel only unlinked exchange orders ─
                if exchange_order_ids:
                    active_db_orders = (
                        session.query(Order.exchange_order_id)
                        .join(Trade, Order.trade_id == Trade.id)
                        .filter(
                            Trade.status.in_(["open", "pending"]),
                            Order.status == "open",
                            Order.exchange_order_id.isnot(None),
                        )
                        .all()
                    )
                    active_order_ids: Set[str] = {
                        row[0] for row in active_db_orders if row[0]
                    }

                    orphaned_ids = exchange_order_ids - active_order_ids

                    if orphaned_ids:
                        logger.warning(
                            "Found %d orphaned order(s) not linked to active trades: %s",
                            len(orphaned_ids), orphaned_ids,
                        )
                        for order_id in orphaned_ids:
                            try:
                                self._exchange.cancel_order(order_id, cfg.trading_symbol)
                                cancelled += 1
                                logger.info("Cancelled orphaned exchange order: %s", order_id)
                            except Exception as exc:
                                logger.warning("Failed to cancel orphaned order %s: %s", order_id, exc)

                            db_order = (
                                session.query(Order)
                                .filter(Order.exchange_order_id == order_id)
                                .first()
                            )
                            if db_order and db_order.status == "open":
                                db_order.status = "cancelled"
                                if db_order.trade_id:
                                    linked_trade = (
                                        session.query(Trade)
                                        .filter(Trade.id == db_order.trade_id)
                                        .first()
                                    )
                                    if linked_trade:
                                        linked_trade.clear_exit_order_id(db_order.type)
                                        logger.info(
                                            "Cleared stale %s ID on trade #%d (orphaned order %s).",
                                            db_order.type, linked_trade.id, order_id,
                                        )

            session.commit()

            if cancelled > 0:
                logger.info("Orphan cleanup: cancelled %d order(s) total.", cancelled)
            else:
                logger.debug("No orphaned orders found.")

            return cancelled

        except Exception as exc:
            logger.error("Error during orphan cleanup: %s", exc, exc_info=True)
            session.rollback()
            return 0
        finally:
            session.close()

    def cleanup_stale_db_orders(self) -> int:
        """Mark DB orders as cancelled if they no longer exist on the exchange.

        This handles orders that were cancelled directly on the exchange
        (e.g. via the web UI) but not reflected in our database.

        Returns:
            Number of stale DB orders updated.
        """
        session = get_session()
        try:
            open_db_orders = (
                session.query(Order)
                .filter(
                    Order.status == "open",
                    Order.exchange_order_id.isnot(None),
                )
                .all()
            )

            if not open_db_orders:
                return 0

            # Fetch all open orders from exchange for comparison
            try:
                exchange_orders = self._exchange.fetch_open_orders(cfg.trading_symbol)
            except Exception as exc:
                logger.error("Failed to fetch open orders for stale check: %s", exc)
                return 0

            exchange_ids: Set[str] = {
                str(o.get("id", "")) for o in exchange_orders if o.get("id")
            }

            stale_count = 0
            for db_order in open_db_orders:
                if db_order.exchange_order_id not in exchange_ids:
                    # This order is no longer on the exchange — check if filled or cancelled
                    try:
                        exch_order = self._exchange.fetch_order(
                            db_order.exchange_order_id, cfg.trading_symbol
                        )
                        status = exch_order.get("status", "")
                        if status == "closed":
                            # It was filled — the normal check_open_trades will handle it
                            continue
                        elif status in ("canceled", "cancelled"):
                            db_order.status = "cancelled"
                            stale_count += 1
                            # Clear the stale reference on the parent Trade
                            if db_order.trade_id:
                                linked_trade = (
                                    session.query(Trade)
                                    .filter(Trade.id == db_order.trade_id)
                                    .first()
                                )
                                if linked_trade:
                                    linked_trade.clear_exit_order_id(db_order.type)
                                    logger.info(
                                        "Cleared stale %s ID on trade #%d "
                                        "(order %s cancelled on exchange).",
                                        db_order.type, linked_trade.id,
                                        db_order.exchange_order_id,
                                    )
                            logger.info(
                                "Stale order %s marked cancelled (was cancelled on exchange).",
                                db_order.exchange_order_id,
                            )
                    except Exception:
                        # Can't fetch the order at all — mark as cancelled
                        db_order.status = "cancelled"
                        stale_count += 1
                        # Clear the stale reference on the parent Trade
                        if db_order.trade_id:
                            linked_trade = (
                                session.query(Trade)
                                .filter(Trade.id == db_order.trade_id)
                                .first()
                            )
                            if linked_trade:
                                linked_trade.clear_exit_order_id(db_order.type)
                                logger.info(
                                    "Cleared stale %s ID on trade #%d "
                                    "(order %s unfetchable).",
                                    db_order.type, linked_trade.id,
                                    db_order.exchange_order_id,
                                )
                        logger.warning(
                            "Stale order %s marked cancelled (could not fetch from exchange).",
                            db_order.exchange_order_id,
                        )

            if stale_count > 0:
                session.commit()
                logger.info("Cleaned up %d stale DB orders.", stale_count)

            return stale_count

        except Exception as exc:
            logger.error("Error cleaning stale DB orders: %s", exc, exc_info=True)
            session.rollback()
            return 0
        finally:
            session.close()

    # ── Position-order reconciliation ─────────────────────────

    def reconcile_positions_and_orders(self) -> bool:
        """Reconcile open positions with their SL/TP orders.

        Ensures exactly 1 SL and 1 TP order exist per open position with
        correct side and size.  Cancels all orders when no position exists.

        When inconsistencies are detected, cancels **every** exchange-side
        exit order (not just DB-tracked ones) before placing fresh SL/TP.
        This prevents ghost orders from accumulating after mid-placement
        crashes.

        Returns:
            ``True`` if state was already consistent (no fixes needed).
            ``False`` if inconsistencies were found and fixed — the caller
            should skip signal evaluation this iteration.
        """
        symbol = cfg.trading_symbol

        # ── Fetch current exchange state ───────────────────────
        try:
            positions = self._exchange.fetch_positions([symbol])
        except Exception as exc:
            logger.error("Reconciliation: failed to fetch positions: %s", exc)
            return True  # can't reconcile — don't block the loop

        try:
            open_orders = self._exchange.fetch_open_orders(symbol)
        except Exception as exc:
            logger.error("Reconciliation: failed to fetch open orders: %s", exc)
            return True

        # Find the active position (if any)
        position = None
        for p in positions:
            if float(p.get("contracts", 0) or 0) > 0:
                position = p
                break

        # ── No position → cancel everything ────────────────────
        if position is None:
            if open_orders:
                logger.warning(
                    "Reconciliation: No open position — cancelling %d orphaned order(s).",
                    len(open_orders),
                )
                try:
                    self._exchange.cancel_all_orders(symbol)
                except Exception as exc:
                    logger.error("Reconciliation: cancel_all_orders failed: %s", exc)
                logger.warning(
                    "No open position found — cancelled %d orphaned orders",
                    len(open_orders),
                )
                return False
            else:
                logger.info("Reconciliation: No position, no orders — clean state.")
                return True

        # ── Position exists — classify existing orders ─────────
        pos_side = position["side"]                       # "long" or "short"
        pos_contracts = int(position["contracts"])
        entry_price = float(position["entryPrice"])
        exit_side = "sell" if pos_side == "long" else "buy"
        trade_side = "buy" if pos_side == "long" else "sell"

        sl_orders = []
        tp_orders = []
        # Collect ALL exit-side orders from the exchange (tracked or not)
        all_exit_orders = []

        for o in open_orders:
            raw = o.get("info", {})
            order_side = o.get("side", "")
            stop_type = raw.get("stop_order_type", "")

            # Only consider orders on the correct exit side
            if order_side != exit_side:
                continue

            all_exit_orders.append(o)

            if stop_type == "stop_loss_order":
                sl_orders.append(o)
            elif stop_type == "take_profit_order":
                tp_orders.append(o)

        # ── Check if SL/TP are correct ─────────────────────────
        sl_ok = (
            len(sl_orders) == 1
            and int(sl_orders[0].get("amount", 0)) == pos_contracts
        )
        tp_ok = (
            len(tp_orders) == 1
            and int(tp_orders[0].get("amount", 0)) == pos_contracts
        )

        if sl_ok and tp_ok:
            sl_price = sl_orders[0].get("price", 0)
            tp_price = tp_orders[0].get("price", 0)
            logger.info(
                "Reconciliation OK: position %s size=%d entry=%.2f | "
                "SL order @ %.2f | TP order @ %.2f",
                pos_side, pos_contracts, entry_price, sl_price, tp_price,
            )
            return True

        # ── Inconsistency detected — fix it ────────────────────
        logger.warning(
            "Reconciliation: inconsistency detected for %s %s position "
            "(size=%d, entry=%.2f). SL orders=%d, TP orders=%d. Fixing...",
            symbol, pos_side, pos_contracts, entry_price,
            len(sl_orders), len(tp_orders),
        )

        # Cancel ALL exchange-side exit orders — not just the ones we
        # track in the DB.  This kills ghost orders left by mid-placement
        # crashes that the DB never recorded.
        all_cancels_ok = True
        for o in all_exit_orders:
            oid = o.get("id", "")
            if oid:
                try:
                    self._exchange.cancel_order(oid, symbol)
                    logger.info(
                        "Reconciliation: cancelled exchange exit order %s "
                        "(ghost-safe sweep)", oid,
                    )
                except Exception as exc:
                    logger.warning(
                        "Reconciliation: failed to cancel order %s: %s", oid, exc,
                    )
                    all_cancels_ok = False

        if not all_cancels_ok:
            logger.error(
                "Reconciliation: not all exit orders could be cancelled — "
                "skipping SL/TP placement to avoid duplicates.",
            )
            return False

        # Re-calculate correct SL/TP prices
        sl_price = self._risk.calculate_stop_loss(entry_price, trade_side)
        tp_price = self._risk.calculate_take_profit(entry_price, trade_side)

        # Find the matching DB trade so we can update sl_order_id / tp_order_id
        session = get_session()
        try:
            db_trade = (
                session.query(Trade)
                .filter(Trade.status == "open", Trade.symbol == symbol)
                .order_by(Trade.entry_time.desc())
                .first()
            )

            # Clear stale exit order IDs before placing new ones
            if db_trade:
                db_trade.clear_exit_order_id("stop_loss")
                db_trade.clear_exit_order_id("take_profit")
                logger.info(
                    "Reconciliation: cleared stale sl_order_id/tp_order_id "
                    "on trade #%d before re-placing.",
                    db_trade.id,
                )

            # Place fresh SL
            new_sl_id = None
            try:
                sl_order = self._exchange.place_stop_order(
                    symbol=symbol,
                    side=exit_side,
                    amount=pos_contracts,
                    stop_price=sl_price,
                    stop_order_type="stop_loss_order",
                    limit_price=sl_price,
                    params={"_is_contracts": True},
                )
                new_sl_id = str(sl_order.get("id", ""))
                logger.info(
                    "Reconciliation: placed SL — %s %d contracts @ %.2f | id=%s",
                    exit_side, pos_contracts, sl_price, new_sl_id,
                )
                # Create DB Order record
                if db_trade:
                    sl_record = Order(
                        trade_id=db_trade.id,
                        exchange_order_id=new_sl_id,
                        type="stop_loss",
                        side=exit_side,
                        price=sl_price,
                        qty=db_trade.qty,
                        status="open",
                    )
                    session.add(sl_record)
            except Exception as exc:
                logger.error("Reconciliation: failed to place SL: %s", exc)

            # Place fresh TP
            new_tp_id = None
            try:
                tp_order = self._exchange.place_stop_order(
                    symbol=symbol,
                    side=exit_side,
                    amount=pos_contracts,
                    stop_price=tp_price,
                    stop_order_type="take_profit_order",
                    limit_price=tp_price,
                    params={"_is_contracts": True},
                )
                new_tp_id = str(tp_order.get("id", ""))
                logger.info(
                    "Reconciliation: placed TP — %s %d contracts @ %.2f | id=%s",
                    exit_side, pos_contracts, tp_price, new_tp_id,
                )
                # Create DB Order record
                if db_trade:
                    tp_record = Order(
                        trade_id=db_trade.id,
                        exchange_order_id=new_tp_id,
                        type="take_profit",
                        side=exit_side,
                        price=tp_price,
                        qty=db_trade.qty,
                        status="open",
                    )
                    session.add(tp_record)
            except Exception as exc:
                logger.error("Reconciliation: failed to place TP: %s", exc)

            # Update Trade record with new SL/TP order IDs and mark old
            # DB orders as cancelled
            if db_trade:
                # Mark old SL/TP Order rows as cancelled
                new_ids = [x for x in [new_sl_id, new_tp_id] if x]
                old_order_filter = (
                    session.query(Order)
                    .filter(
                        Order.trade_id == db_trade.id,
                        Order.type.in_(["stop_loss", "take_profit"]),
                        Order.status == "open",
                    )
                )
                if new_ids:
                    old_order_filter = old_order_filter.filter(
                        Order.exchange_order_id.notin_(new_ids)
                    )
                old_orders = old_order_filter.all()
                for old_o in old_orders:
                    old_o.status = "cancelled"

                if new_sl_id:
                    db_trade.sl_order_id = new_sl_id
                if new_tp_id:
                    db_trade.tp_order_id = new_tp_id
                session.commit()
                logger.info(
                    "Reconciliation: updated trade #%d — sl_order_id=%s, tp_order_id=%s",
                    db_trade.id, new_sl_id, new_tp_id,
                )
            else:
                session.commit()
                logger.warning(
                    "Reconciliation: no matching DB trade found for %s %s position. "
                    "SL/TP placed on exchange but not linked to a trade.",
                    symbol, pos_side,
                )
        except Exception as exc:
            logger.error("Reconciliation: DB update failed: %s", exc)
            session.rollback()
        finally:
            session.close()

        return False  # Fixed inconsistencies — skip signal evaluation

    # ── Kill switch / shutdown ──────────────────────────────

    def execute_kill_switch(self) -> None:
        """Cancel all orders and close all positions (kill switch)."""
        logger.critical("KILL SWITCH ENGAGED — closing everything.")
        telegram_alerts.alert_kill_switch()

        # Cancel all open orders
        try:
            self._exchange.cancel_all_orders(cfg.trading_symbol)
        except Exception as exc:
            logger.error("Error cancelling orders during kill switch: %s", exc)

        # Close all open positions
        try:
            positions = self._exchange.fetch_positions([cfg.trading_symbol])
            for pos in positions:
                contracts = float(pos.get("contracts", 0) or 0)
                side = pos.get("side", "")
                if contracts > 0 and side:
                    pos_side = "buy" if side == "long" else "sell"
                    self._exchange.close_position(cfg.trading_symbol, pos_side, contracts)
                    logger.info("Closed position: %s %.6f", side, contracts)
        except Exception as exc:
            logger.error("Error closing positions during kill switch: %s", exc)

        # Update DB: mark all open/pending trades and orders
        session = get_session()
        try:
            active_trades = (
                session.query(Trade)
                .filter(Trade.status.in_(["open", "pending"]))
                .all()
            )
            for trade in active_trades:
                trade.status = "killed"
                trade.exit_time = datetime.now(timezone.utc)
                trade.exit_reason = "kill_switch"

            active_orders = (
                session.query(Order)
                .filter(Order.status == "open")
                .all()
            )
            for order in active_orders:
                order.status = "cancelled"

            session.commit()
        except Exception as exc:
            logger.error("Error updating DB during kill switch: %s", exc)
            session.rollback()
        finally:
            session.close()

    def cancel_unfilled_entries(self) -> None:
        """Cancel all unfilled entry orders (used during graceful shutdown)."""
        session = get_session()
        try:
            pending_trades = (
                session.query(Trade)
                .filter(Trade.status == "pending")
                .all()
            )
            for trade in pending_trades:
                entry_orders = (
                    session.query(Order)
                    .filter(
                        Order.trade_id == trade.id,
                        Order.type == "limit",
                        Order.status == "open",
                    )
                    .all()
                )
                for order in entry_orders:
                    self._cancel_entry(session, trade, order, "bot shutdown")

            logger.info("All unfilled entry orders cancelled for shutdown.")
        except Exception as exc:
            logger.error("Error cancelling entries on shutdown: %s", exc)
            session.rollback()
        finally:
            session.close()

    # ── State reload after restart ──────────────────────────

    def reload_state(self) -> None:
        """Reload pending entry tracking state from the database after restart.

        Re-populates the candle counter for any pending entries so the
        expiry logic works correctly after a bot restart.  Also runs
        orphaned order cleanup.
        """
        session = get_session()
        try:
            pending_trades = (
                session.query(Trade)
                .filter(Trade.status == "pending")
                .all()
            )
            for trade in pending_trades:
                # We don't know exactly how many candles have passed,
                # so start at 0 — worst case they get one extra candle.
                self._pending_entry_candle_counts[trade.id] = 0
                logger.info(
                    "Reloaded pending trade: id=%d, side=%s, qty=%.6f",
                    trade.id,
                    trade.side,
                    trade.qty,
                )
            logger.info(
                "State reloaded: %d pending entries, %d open trades.",
                len(pending_trades),
                session.query(Trade).filter(Trade.status == "open").count(),
            )
        finally:
            session.close()

        # Clean up orphaned orders left behind by a previous unclean shutdown
        orphaned = self.cleanup_orphaned_orders()
        if orphaned > 0:
            logger.warning(
                "Startup cleanup: cancelled %d orphaned order(s) on exchange.",
                orphaned,
            )
