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
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Optional, List

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
        self._closing = asyncio.Event()
        self._refresh_task: asyncio.Task | None = None

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
            last = getattr(self._market, "last_price", None) or getattr(
                self._market, "oracle_price", None
            )
            if last is not None:
                mid = Decimal(str(last))
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

        self._slots = []
        for i in range(self.level_count):
            raw = self.min_price + self.grid_step * (i + 1)
            side = OrderSide.BUY if raw <= mid else OrderSide.SELL
            price = raw.quantize(
                self._tick, rounding=ROUND_FLOOR if side == OrderSide.BUY else ROUND_CEILING
            )
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
            if not result or getattr(result, "error", None) or not getattr(result, "data", None):
                logger.error(
                    "create_and_place_order failed | market=%s side=%s idx=%d price=%s prev_id=%s ext_id=%s error=%s data=%s",
                    self._market.name if self._market else "?",
                    side.name,
                    idx,
                    str(price),
                    previous_id,
                    new_external_id,
                    getattr(result, "error", None) if result else None,
                    getattr(result, "data", None) if result else None,
                )
                raise RuntimeError(getattr(result, "error", "empty response"))
            status = getattr(getattr(result, "data", None), "status", None)
            if status != "PLACED":
                logger.error(
                    "order rejected | market=%s side=%s idx=%d price=%s status=%s",
                    self._market.name if self._market else "?",
                    side.name,
                    idx,
                    str(price),
                    status,
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
        except Exception as e:
            if not self._is_edit_not_found(e):
                logger.exception(
                    "order cancel failed | market=%s idx=%d ext_id=%s",
                    self._market.name if self._market else "?",
                    idx,
                    slot.external_id,
                )
        slots[idx] = Slot(None, slot.price, slot.side)

    async def _switch_slot_to_side(self, idx: int, side: OrderSide) -> None:
        slot = self._slots[idx]
        if slot.side == side:
            return
        await self._cancel_slot(self._slots, idx)
        self._slots[idx] = Slot(None, slot.price, side)

    async def _on_fill(self, idx: int, side: OrderSide) -> None:
        price = self._slots[idx].price
        # deactivate the filled level while waiting for the opposing trade
        self._slots[idx] = Slot(None, price, None)
        if side == OrderSide.BUY:
            if idx + 1 < len(self._slots):
                await self._switch_slot_to_side(idx + 1, OrderSide.SELL)
                raw = self.min_price + self.grid_step * (idx + 2)
                sell_price = raw.quantize(self._tick, rounding=ROUND_CEILING)
                self._slots[idx + 1] = Slot(None, sell_price, OrderSide.SELL)
                await self._ensure_order(
                    self._slots, OrderSide.SELL, idx + 1, sell_price
                )
        elif side == OrderSide.SELL and idx > 0:
            await self._switch_slot_to_side(idx - 1, OrderSide.BUY)
            await self._ensure_order(self._slots, OrderSide.BUY, idx - 1, self._slots[idx - 1].price)

    async def _update_grid(self) -> None:
        if self._tick is None:
            return

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
                if not ext_id or ext_id not in expected_ids:
                    async def _cancel(order=order):
                        if getattr(order, "external_id", None):
                            return await self.client.cancel_order(order_external_id=order.external_id)
                        return await self.client.cancel_order(order_id=order.id)
                    try:
                        await call_with_retries(_cancel, limiter=self._limiter)
                        logger.warning(
                            "reconcile cancel orphan order | market=%s ext_id=%s id=%s",
                            self._market.name if self._market else "?",
                            ext_id,
                            getattr(order, "id", None),
                        )
                    except Exception:
                        logger.exception(
                            "reconcile cancel failed | market=%s ext_id=%s id=%s",
                            self._market.name if self._market else "?",
                            ext_id,
                            getattr(order, "id", None),
                        )
                else:
                    open_ids.add(ext_id)
        except Exception:
            pass

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

        await asyncio.gather(*tasks)


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
