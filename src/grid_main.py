# grid_main.py
"""Simple symmetric grid trading bot for Extended / X10 perpetuals.

This module maintains a ladder of buy and sell orders spaced at fixed USD
intervals around the mid price.  Orders are refreshed whenever the mid price
moves or an existing order is filled.  The implementation mirrors the style of
``maker_main`` and ``hybrid_main`` for consistency.
"""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Optional, List, Dict
from datetime import datetime, timezone

from x10.perpetual.orderbook import OrderBook
from x10.perpetual.orders import OrderSide
from x10.perpetual.simple_client.simple_trading_client import BlockingTradingClient

from account import TradingAccount
from rate_limit import build_rate_limiter
from backoff_utils import call_with_retries
from id_generator import uuid_external_id
from utils import logger, setup_logging, _close_order_book
import logging

# --- Configuration ----------------------------------------------------------------------

# Expose a few basic parameters via environment variables so the bot can be
# configured without modifying the source.  If ``GRID_MARKET`` is not provided
# interactively ask the user which market to trade on.
MARKET_NAME = os.getenv("GRID_MARKET") or input("Market ? ")
# Parse level count robustly: avoid int(None) when env is unset.  The value
# represents the total number of grid levels across both sides.
GRID_LEVELS = int(os.getenv("GRID_LEVELS") or input("How many total levels? "))
GRID_SIZE_USD = Decimal(os.getenv("GRID_SIZE_USD", "25"))
GRID_MIN_PRICE = Decimal(os.getenv("GRID_MIN_PRICE") or input("Min Price ? "))
GRID_MAX_PRICE = Decimal(os.getenv("GRID_MAX_PRICE") or input("Max Price ? "))
REFRESH_INTERVAL_SEC = float(os.getenv("GRID_REFRESH_SEC", "5"))


@dataclass
class Slot:
    """Track one outstanding order at a fixed grid level."""

    external_id: Optional[str]
    price: Decimal
    side: Optional[OrderSide]


class GridTrader:
    """Very small grid trading helper.

    ``GRID_LEVELS`` represents the total number of price levels across the
    entire grid.  Levels below the midpoint start as buy orders and those above
    as sell orders.  When a buy order fills, the next higher level flips to a
    sell order; once that sell is executed, the original level becomes a buy
    again.  This keeps the grid's price points static while alternating sides
    after each fill.

    Orders are sized in USD notional and converted to synthetic amount using the
    market's trading configuration.
    """

    def __init__(
        self,
        account: TradingAccount,
        market_name: str,
        grid_step: Decimal,
        level_count: int,
        order_size_usd: Decimal,
        lower_bound: Decimal,
        upper_bound: Decimal,
    ):
        self.account = account
        self.market_name = market_name
        self.grid_step = grid_step
        self.level_count = level_count
        self.order_size_usd = order_size_usd
        self.min_price = lower_bound
        self.max_price = upper_bound

        self.client: BlockingTradingClient = account.get_blocking_client()
        self._endpoint_config = account.endpoint_config
        self._limiter = build_rate_limiter()

        # Single list of slots covering the entire price range.  ``_buy_slots``
        # is kept as an alias for backward compatibility with unit tests which
        # reference it directly.  Populate with dummy buy slots so tests may
        # invoke ``_ensure_order`` before ``start`` is called.
        self._slots: List[Slot] = [
            Slot(None, lower_bound, OrderSide.BUY) for _ in range(level_count)
        ]
        self._buy_slots = self._slots  # type: ignore[assignment]
        self._placement_lock = asyncio.Lock()

        self._order_book: OrderBook | None = None
        self._market = None
        self._tick: Decimal | None = None
        # Precomputed, tick-aligned prices for each grid level.  Populated in
        # ``start`` once the market tick size is known.  Tests that call
        # ``_on_fill`` directly may trigger a lazy initialization if this list
        # is still empty.
        self._level_prices: List[Decimal] = []
        self._closing = asyncio.Event()
        self._refresh_task: asyncio.Task | None = None

        # Track recently created orders to avoid cancelling them before they
        # are recorded in ``_slots``.
        self._recent_orders: Dict[str, float] = {}
        self._orphan_grace = 2.0  # seconds

    # ------------------------------------------------------------------
    async def _create_order_book(self) -> OrderBook:
        return await OrderBook.create(
            self._endpoint_config,
            market_name=self._market.name,
            start=True,
        )

    @staticmethod
    def get_tick(cfg) -> Decimal:
        min_change = getattr(cfg, "min_price_change", None)
        if min_change is not None:
            return Decimal(str(min_change))
        prec = getattr(cfg, "price_precision", 2)
        return Decimal(1).scaleb(-prec)

    # ------------------------------------------------------------------
    def _build_price_levels(self) -> None:
        """Precompute all grid level prices aligned to the market tick.

        ``grid_step`` may not be an exact multiple of the tick size; in that
        case we snap it to the nearest tick to avoid cumulative drift when
        repeatedly adding the step.  The resulting list is stored in
        ``self._level_prices`` and reused for subsequent order placements.
        """

        if self._tick is None:
            return

        if self.grid_step % self._tick != 0:
            snapped = (
                (self.grid_step / self._tick)
                .to_integral_value(rounding=ROUND_HALF_UP)
                * self._tick
            )
            logger.warning(
                "grid_step adjusted to tick | from=%s to=%s tick=%s",
                str(self.grid_step),
                str(snapped),
                str(self._tick),
            )
            self.grid_step = snapped

        levels: List[Decimal] = []
        price = self.min_price + self.grid_step
        for _ in range(self.level_count):
            if price > self.max_price:
                break
            levels.append(price.quantize(self._tick, rounding=ROUND_HALF_UP))
            price += self.grid_step

        self._level_prices = levels
        self.level_count = len(levels)

    # ------------------------------------------------------------------
    async def start(self) -> None:
        setup_logging(logging.INFO)
        markets = await self.client.get_markets()
        if self.market_name not in markets:
            raise RuntimeError(f"Market {self.market_name} introuvable.")
        self._market = markets[self.market_name]
        self._tick = self.get_tick(self._market.trading_config)

        # Obtain a live mid price from the order book (or last trade) before
        # building the grid.  The order book is kept for later refreshes.
        self._order_book = await self._create_order_book()
        bid_fn = getattr(self._order_book, "best_bid", None)
        ask_fn = getattr(self._order_book, "best_ask", None)
        bid = bid_fn() if bid_fn else None
        ask = ask_fn() if ask_fn else None
        mid: Decimal | None = None
        if bid and ask:
            mid = (Decimal(bid.price) + Decimal(ask.price)) / 2
        else:
            stats = getattr(self._market, "market_stats", None) or getattr(
                self._market, "marketStats", None
            )
            if stats:
                bid_px = getattr(stats, "bidPrice", None) or getattr(
                    stats, "bid_price", None
                )
                ask_px = getattr(stats, "askPrice", None) or getattr(
                    stats, "ask_price", None
                )
                if bid_px is not None and ask_px is not None:
                    try:
                        mid = (Decimal(str(bid_px)) + Decimal(str(ask_px))) / 2
                    except Exception:
                        mid = None
                if mid is None:
                    for attr in (
                        "markPrice",
                        "mark_price",
                        "indexPrice",
                        "index_price",
                        "lastPrice",
                        "last_price",
                    ):
                        raw = getattr(stats, attr, None)
                        if raw is not None:
                            try:
                                mid = Decimal(str(raw))
                                break
                            except Exception:
                                mid = None
            
        if mid is None:
            # Final fallback: query ticker or recent trades via the client
            price: Decimal | None = None
            ticker_fn = getattr(self.client, "get_market_ticker", None)
            if ticker_fn:
                try:
                    ticker = await ticker_fn(self._market.name)
                    raw = getattr(ticker, "price", None) or getattr(
                        ticker, "last_price", None
                    )
                    if raw is not None:
                        price = Decimal(str(raw))
                except Exception:
                    price = None
            if price is None:
                trades_fn = getattr(self.client, "get_trades", None)
                if trades_fn:
                    try:
                        trades = await trades_fn(self._market.name, limit=1)
                        first = None
                        if isinstance(trades, list):
                            first = trades[0] if trades else None
                        else:
                            data = getattr(trades, "data", None)
                            first = data[0] if data else None
                        raw = getattr(first, "price", None) if first else None
                        if raw is not None:
                            price = Decimal(str(raw))
                    except Exception:
                        price = None
            mid = price
        if mid is None:
            if self._order_book:
                await _close_order_book(self._order_book)
            raise RuntimeError("Unable to determine current mid price")

        if mid <= self.min_price or mid >= self.max_price:
            logger.warning(
                "mid price outside configured bounds | mid=%s min=%s max=%s",
                str(mid),
                str(self.min_price),
                str(self.max_price),
            )
            await _close_order_book(self._order_book)
            self._order_book = None
            raise RuntimeError("grid bounds do not bracket current price")
        # Precompute all tick-aligned levels and assign sides based on the live
        # mid price.  ``_level_prices`` is reused for subsequent repopulation so
        # levels remain consistent across fills.
        self._build_price_levels()
        self._slots = []
        for price in self._level_prices:
            side = OrderSide.BUY if price <= mid else OrderSide.SELL
            self._slots.append(Slot(None, price, side))
        self._buy_slots = self._slots  # alias rebuild after reassignment

        # Clear any stale orders from previous runs
        await call_with_retries(
            lambda: self.client.mass_cancel(markets=[self._market.name]),
            limiter=self._limiter,
        )

        await self._update_grid()
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        self._closing.set()
        try:
            await call_with_retries(
                lambda: self.client.mass_cancel(markets=[self._market.name]),
                limiter=self._limiter,
            )
        except Exception:
            pass

        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        if self._order_book:
            await _close_order_book(self._order_book)
        await self.account.close()

    async def _refresh_loop(self) -> None:
        while not self._closing.is_set():
            try:
                await self._update_grid()
                active_buys = sum(
                    1 for s in self._slots if s.side == OrderSide.BUY and s.external_id
                )
                active_sells = sum(
                    1 for s in self._slots if s.side == OrderSide.SELL and s.external_id
                )
                expected_total = max(0, len(self._slots) - 1)
                if (active_buys + active_sells) < expected_total:
                    logger.warning(
                        "grid refresh | active_total=%d expected_total=%d below expected; repopulating",
                        active_buys + active_sells,
                        expected_total,
                    )
                    await self._update_grid()
                    active_buys = sum(
                        1 for s in self._slots if s.side == OrderSide.BUY and s.external_id
                    )
                    active_sells = sum(
                        1 for s in self._slots if s.side == OrderSide.SELL and s.external_id
                    )
                logger.info(
                    "grid refresh | active_buys=%d active_sells=%d",
                    active_buys,
                    active_sells,
                )
                await asyncio.sleep(REFRESH_INTERVAL_SEC)
            except Exception:
                logger.exception("refresh failed", exc_info=True)
                await asyncio.sleep(0.5)
                continue

    # ------------------------------------------------------------------
    def _is_edit_not_found(self, e: Exception) -> bool:
        s = str(e)
        return "Edit order not found" in s or '"code":1142' in s or "not found" in s.lower()

    async def _ensure_order(
        self, slots: List[Slot], side: OrderSide, idx: int, price: Decimal
    ) -> None:
        slot = slots[idx]
        if slot.external_id and slot.side == side:
            return

        if self._tick is not None:
            adj = price.quantize(self._tick, rounding=ROUND_HALF_UP)
            if adj < self.min_price or adj > self.max_price:
                logger.warning(
                    "adjusted price out of bounds | market=%s side=%s idx=%d price=%s tick=%s",
                    self._market.name if self._market else "?",
                    side.name,
                    idx,
                    str(adj),
                    str(self._tick),
                )
                slots[idx] = Slot(None, price, side)
                return
            if adj != price:
                logger.info(
                    "price adjusted to tick | market=%s side=%s idx=%d from=%s to=%s tick=%s",
                    self._market.name if self._market else "?",
                    side.name,
                    idx,
                    str(price),
                    str(adj),
                    str(self._tick),
                )
            price = adj

        def _order_size(px: Decimal) -> Decimal:
            return self._market.trading_config.calculate_order_size_from_value(
                self.order_size_usd, px
            )

        synthetic = _order_size(price)
        min_size = self._market.trading_config.min_order_size
        if synthetic < min_size:
            return

        new_external_id = uuid_external_id("grid", side.name.lower(), idx)
        previous_id = slot.external_id
        self._recent_orders[new_external_id] = asyncio.get_running_loop().time()

        async def _place(prev_id: Optional[str], px: Decimal, s: OrderSide):
            size = _order_size(px)
            if size < min_size:
                return None
            return await self.client.create_and_place_order(
                market_name=self._market.name,
                amount_of_synthetic=size,
                price=px,
                side=s,
                post_only=True,
                previous_order_external_id=prev_id,
                external_id=new_external_id,
            )

        try:
            async with self._placement_lock:
                result = await call_with_retries(
                    lambda: _place(previous_id, price, side), limiter=self._limiter
                )
            if getattr(result, "error", None):
                logger.error(
                    "create_and_place_order failed | market=%s side=%s idx=%d price=%s prev_id=%s ext_id=%s error=%s",
                    self._market.name if self._market else "?",
                    side.name,
                    idx,
                    str(price),
                    previous_id,
                    new_external_id,
                    getattr(result, "error", None),
                )
                raise RuntimeError(getattr(result, "error", "unknown"))

            order = getattr(result, "data", None)
            if order is None and getattr(result, "status", None):
                order = result

            if order is None:
                logger.error(
                    "create_and_place_order returned no data | market=%s side=%s idx=%d price=%s prev_id=%s ext_id=%s result=%s",
                    self._market.name if self._market else "?",
                    side.name,
                    idx,
                    str(price),
                    previous_id,
                    new_external_id,
                    result,
                )
                slots[idx] = Slot(None, price, side)
                self._recent_orders.pop(new_external_id, None)
                return

            status = getattr(order, "status", None)
            accepted_statuses = {"PLACED", "NEW", "OPEN", "ACCEPTED"}
            if status not in accepted_statuses:
                reason = (
                    getattr(order, "reason", None)
                    or getattr(order, "cancel_reason", None)
                    or getattr(order, "cancelReason", None)
                )
                logger.error(
                    "order rejected | market=%s side=%s idx=%d price=%s status=%s reason=%s",
                    self._market.name if self._market else "?",
                    side.name,
                    idx,
                    str(price),
                    status,
                    reason,
                )
                retry_msg = (str(reason).lower() if reason else "")
                if (
                    status in ("REJECTED", "CANCELED")
                    and self._tick is not None
                    and (
                        "post-only" in retry_msg
                        or "would cross" in retry_msg
                        or "immediate match" in retry_msg
                    )
                ):
                    adj = price + (
                        self._tick if side == OrderSide.SELL else -self._tick
                    )
                    if (
                        adj % self._tick != 0
                        or adj < self.min_price
                        or adj > self.max_price
                    ):
                        logger.warning(
                            "adjusted price out of bounds | market=%s side=%s idx=%d price=%s tick=%s",
                            self._market.name if self._market else "?",
                            side.name,
                            idx,
                            str(adj),
                            str(self._tick),
                        )
                    else:
                        try:
                            async with self._placement_lock:
                                result2 = await call_with_retries(
                                    lambda: _place(None, adj, side),
                                    limiter=self._limiter,
                                )
                            order2 = getattr(result2, "data", None)
                            if order2 is None and getattr(result2, "status", None):
                                order2 = result2
                            if getattr(order2, "status", None) in accepted_statuses:
                                slots[idx] = Slot(new_external_id, adj, side)
                                return
                        except Exception:
                            logger.exception(
                                "order placement post-only adjust failed | market=%s side=%s idx=%d price=%s",
                                self._market.name if self._market else "?",
                                side.name,
                                idx,
                                str(price),
                            )
                if previous_id:
                    try:
                        await self._cancel_slot(slots, idx)
                    except Exception:
                        logger.exception(
                            "previous order cancel failed | market=%s side=%s idx=%d ext_id=%s",
                            self._market.name if self._market else "?",
                            side.name,
                            idx,
                            previous_id,
                        )
                slots[idx] = Slot(None, price, side)
                self._recent_orders.pop(new_external_id, None)
                return
            slots[idx] = Slot(new_external_id, price, side)
        except Exception as e:
            msg = str(e).lower()
            # If backend reports the same order hash was already placed, assume
            # it succeeded on a previous attempt and reconcile by querying it.
            if "hash already placed" in msg:
                try:
                    async_client = self.account.get_async_client()
                    resp = await call_with_retries(
                        lambda: async_client.account.get_open_orders(
                            market_names=[self._market.name]
                        ),
                        limiter=self._limiter,
                    )
                    open_orders = resp.data or []
                    # Filter locally by external_id to support SDKs
                    for order in open_orders:
                        if getattr(order, "external_id", None) == new_external_id:
                            slots[idx] = Slot(order.external_id, order.price, side)
                            return
                    # Not found by external_id; as a fallback, check for same side/price
                    for order in open_orders:
                        if getattr(order, "price", None) == price and getattr(order, "side", None) in (getattr(side, "name", None), side):
                            slots[idx] = Slot(order.external_id, order.price, side)
                            return
                    return
                except Exception as e3:
                    logger.exception(
                        "reconcile after hash-duplicate failed | market=%s side=%s idx=%d ext_id=%s",
                        self._market.name if self._market else "?",
                        side.name,
                        idx,
                        new_external_id,
                    )
                # Could not confirm; clear slot to retry later
                if previous_id:
                    try:
                        await self._cancel_slot(slots, idx)
                    except Exception:
                        logger.exception(
                            "previous order cancel failed | market=%s side=%s idx=%d ext_id=%s",
                            self._market.name if self._market else "?",
                            side.name,
                            idx,
                            previous_id,
                        )
                slots[idx] = Slot(None, price, side)
                self._recent_orders.pop(new_external_id, None)
                return
            # Known SDK race: transient RuntimeError("Lock is not acquired")
            elif isinstance(e, RuntimeError) and "lock is not acquired" in msg:
                # Treat as transient; try again on next refresh without clearing the slot
                logger.warning("transient SDK lock glitch; will retry | side=%s idx=%d", side.name, idx)
                return
            elif self._is_edit_not_found(e):
                # Previous external_id not recognised → send a fresh create at the same price/side
                try:
                    async with self._placement_lock:
                        await call_with_retries(
                            lambda: _place(None, price, side),
                            limiter=self._limiter,
                        )
                    slots[idx] = Slot(new_external_id, price, side)
                    return
                except Exception as e2:
                    logger.exception(
                        "order placement fresh-create failed | market=%s side=%s idx=%d price=%s",
                        self._market.name if self._market else "?",
                        side.name,
                        idx,
                        str(price),
                    )
            # Post-only rejections: nudge away by one tick and try once
            elif ("post-only" in msg) or ("would cross" in msg) or ("immediate match" in msg):
                if self._tick is not None:
                    adj = price + (self._tick if side == OrderSide.SELL else -self._tick)
                    if (
                        adj % self._tick != 0
                        or adj < self.min_price
                        or adj > self.max_price
                    ):
                        logger.warning(
                            "adjusted price out of bounds | market=%s side=%s idx=%d price=%s tick=%s",
                            self._market.name if self._market else "?",
                            side.name,
                            idx,
                            str(adj),
                            str(self._tick),
                        )
                    else:
                        try:
                            async with self._placement_lock:
                                await call_with_retries(
                                    lambda: _place(None, adj, side),
                                    limiter=self._limiter,
                                )
                            slots[idx] = Slot(new_external_id, adj, side)
                            return
                        except Exception:
                            logger.exception(
                                "order placement post-only adjust failed | market=%s side=%s idx=%d price=%s",
                                self._market.name if self._market else "?",
                                side.name,
                                idx,
                                str(price),
                            )
            # Invalid price: either adjust to nearest tick or drop the slot
            elif getattr(e, "code", None) == 1141 or "invalid price value" in msg:
                if self._tick is not None:
                    adj = (price / self._tick).to_integral_value(rounding=ROUND_HALF_UP) * self._tick
                    if (
                        adj % self._tick != 0
                        or adj < self.min_price
                        or adj > self.max_price
                    ):
                        logger.warning(
                            "adjusted price out of bounds | market=%s side=%s idx=%d price=%s tick=%s",
                            self._market.name if self._market else "?",
                            side.name,
                            idx,
                            str(adj),
                            str(self._tick),
                        )
                        slots[idx] = Slot(None, price, side)
                        return
                    try:
                        async with self._placement_lock:
                            await call_with_retries(
                                lambda: _place(None, adj, side),
                                limiter=self._limiter,
                            )
                        logger.warning(
                            "invalid price adjusted | market=%s side=%s idx=%d from=%s to=%s",
                            self._market.name if self._market else "?",
                            side.name,
                            idx,
                            str(price),
                            str(adj),
                        )
                        slots[idx] = Slot(new_external_id, adj, side)
                        return
                    except Exception:
                        logger.exception(
                            "order placement invalid-price adjust failed | market=%s side=%s idx=%d price=%s",
                            self._market.name if self._market else "?",
                            side.name,
                            idx,
                            str(price),
                        )
                logger.warning(
                    "invalid price value | market=%s side=%s idx=%d price=%s",
                    self._market.name if self._market else "?",
                    side.name,
                    idx,
                    str(price),
                )
                slots[idx] = Slot(None, price, side)
                return
            else:
                logger.exception(
                    "order placement failed | market=%s side=%s idx=%d price=%s prev_id=%s ext_id=%s",
                    self._market.name if self._market else "?",
                    side.name,
                    idx,
                    str(price),
                    previous_id,
                    new_external_id,
                )
            if previous_id:
                try:
                    await self._cancel_slot(slots, idx)
                except Exception:
                    logger.exception(
                        "previous order cancel failed | market=%s side=%s idx=%d ext_id=%s",
                        self._market.name if self._market else "?",
                        side.name,
                        idx,
                        previous_id,
                    )
            slots[idx] = Slot(None, price, side)

    async def _cancel_slot(self, slots: List[Slot], idx: int) -> None:
        slot = slots[idx]
        if not slot.external_id:
            slots[idx] = Slot(None, slot.price, slot.side)
            return

        async def _cancel():
            return await self.client.cancel_order(order_external_id=slot.external_id)

        try:
            await call_with_retries(_cancel, limiter=self._limiter)
            slots[idx] = Slot(None, slot.price, slot.side)
        except Exception as e:
            if not self._is_edit_not_found(e):
                logger.exception(
                    "order cancel failed | market=%s idx=%d ext_id=%s",
                    self._market.name if self._market else "?",
                    idx,
                    slot.external_id,
                )
                if (
                    slot.side
                    and self.min_price <= slot.price <= self.max_price
                ):
                    try:
                        await self._ensure_order(
                            self._slots, slot.side, idx, slot.price
                        )
                        return
                    except Exception:
                        logger.exception(
                            "order regeneration failed | market=%s idx=%d side=%s price=%s",
                            self._market.name if self._market else "?",
                            idx,
                            slot.side.name if slot.side else None,
                            str(slot.price),
                        )
            slots[idx] = Slot(None, slot.price, slot.side)

    async def _switch_slot_to_side(self, idx: int, side: OrderSide) -> None:
        slot = self._slots[idx]
        if slot.side == side:
            return
        await self._cancel_slot(self._slots, idx)
        self._slots[idx] = Slot(None, slot.price, side)

    async def _on_fill(self, idx: int, side: OrderSide) -> None:
        if not self._level_prices and self._tick is not None:
            self._build_price_levels()
        price = self._slots[idx].price
        # deactivate the filled level while waiting for the opposing trade
        self._slots[idx] = Slot(None, price, None)
        if side == OrderSide.BUY:
            if idx + 1 < len(self._slots):
                await self._switch_slot_to_side(idx + 1, OrderSide.SELL)
                sell_price = self._level_prices[idx + 1]
                self._slots[idx + 1] = Slot(None, sell_price, OrderSide.SELL)
                await self._ensure_order(
                    self._slots, OrderSide.SELL, idx + 1, sell_price
                )
        elif side == OrderSide.SELL and idx > 0:
            await self._switch_slot_to_side(idx - 1, OrderSide.BUY)
            buy_price = self._level_prices[idx - 1]
            await self._ensure_order(
                self._slots, OrderSide.BUY, idx - 1, buy_price
            )

    async def _update_grid(self) -> None:
        if self._tick is None:
            return

        now = asyncio.get_running_loop().time()
        # prune expired recent orders
        for ext_id, ts in list(self._recent_orders.items()):
            if now - ts > self._orphan_grace:
                self._recent_orders.pop(ext_id, None)

        open_ids: set[str] = set()
        try:
            async_client = self.account.get_async_client()
            resp = await call_with_retries(
                lambda: async_client.account.get_open_orders(market_names=[self._market.name]),
                limiter=self._limiter,
            )
            open_orders = resp.data or []
            expected_ids = {
                s.external_id for s in self._slots if getattr(s, "external_id", None)
            }
            for order in open_orders:
                ext_id = getattr(order, "external_id", None)
                ts = self._recent_orders.get(ext_id)
                if ts is not None and now - ts < self._orphan_grace:
                    logger.debug(
                        "skip orphan cancel | market=%s ext_id=%s age=%.2f",
                        self._market.name if self._market else "?",
                        ext_id,
                        now - ts,
                    )
                    continue
                if not ext_id or ext_id not in expected_ids:
                    async def _cancel(order=order):
                        if getattr(order, "external_id", None):
                            return await self.client.cancel_order(order_external_id=order.external_id)
                        return await self.client.cancel_order(order_id=order.id)
                    try:
                        await call_with_retries(_cancel, limiter=self._limiter)
                        slot_idx = next(
                            (i for i, s in enumerate(self._slots) if s.external_id == ext_id),
                            None,
                        )
                        logger.warning(
                            "reconcile cancel orphan order | market=%s ext_id=%s id=%s slot=%s ts=%s",
                            self._market.name if self._market else "?",
                            ext_id,
                            getattr(order, "id", None),
                            slot_idx,
                            datetime.now(timezone.utc).isoformat(),
                        )
                    except Exception:
                        slot_idx = next(
                            (i for i, s in enumerate(self._slots) if s.external_id == ext_id),
                            None,
                        )
                        logger.exception(
                            "reconcile cancel failed | market=%s ext_id=%s id=%s slot=%s ts=%s",
                            self._market.name if self._market else "?",
                            ext_id,
                            getattr(order, "id", None),
                            slot_idx,
                            datetime.now(timezone.utc).isoformat(),
                        )
                else:
                    open_ids.add(ext_id)
        except Exception:
            logger.exception(
                "reconcile fetch open orders failed | market=%s ts=%s",
                self._market.name if self._market else "?",
                datetime.now(timezone.utc).isoformat(),
            )
            # Avoid triggering slot cancellation when we couldn't retrieve the
            # current order state.  A brief pause reduces noisy logs if the
            # failure persists.
            await asyncio.sleep(0.1)
            return

        tasks = []
        for i, slot in enumerate(self._slots):
            price = slot.price
            if slot.side is None:
                continue
            if slot.external_id and slot.external_id not in open_ids:
                await self._on_fill(i, slot.side)
                slot = self._slots[i]
                if slot.side is None:
                    continue
            if self.min_price <= price <= self.max_price:
                tasks.append(self._ensure_order(self._slots, slot.side, i, price))
            else:
                tasks.append(self._cancel_slot(self._slots, i))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for idx, result in enumerate(results):
            if isinstance(result, Exception):
                logger.exception("[update_grid #%d] task failed", idx, exc_info=result)


# ----------------------------------------------------------------------
async def main():
    # Ensure logging is configured so warnings/errors are visible
    setup_logging(logging.INFO)
    account = TradingAccount()
    # Spread the entire price range across the total number of levels
    grid_step = (GRID_MAX_PRICE - GRID_MIN_PRICE) / GRID_LEVELS
    trader = GridTrader(
        account=account,
        market_name=MARKET_NAME,
        grid_step=grid_step,
        level_count=GRID_LEVELS,
        order_size_usd=GRID_SIZE_USD,
        lower_bound=GRID_MIN_PRICE,
        upper_bound=GRID_MAX_PRICE,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(trader.stop()))
        except NotImplementedError:
            # Fallback for platforms without loop signal handlers (e.g. Windows)
            # Use a sync signal handler to request shutdown via the closing flag;
            # the main loop will exit and the finally block will call stop().
            signal.signal(sig, lambda s, f, lp=loop: lp.call_soon_threadsafe(trader._closing.set))

    await trader.start()
    logger.info("[grid] started on %s", MARKET_NAME)

    try:
        while not trader._closing.is_set():
            await asyncio.sleep(1.0)
    finally:
        await trader.stop()
        logger.info("[grid] stopped.")


if __name__ == "__main__":
    asyncio.run(main())
