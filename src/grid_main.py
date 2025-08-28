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
from utils import logger

# --- Configuration ----------------------------------------------------------------------

# Expose a few basic parameters via environment variables so the bot can be
# configured without modifying the source.  If ``GRID_MARKET`` is not provided
# interactively ask the user which market to trade on.
MARKET_NAME = os.getenv("GRID_MARKET") or input("Market ? ")
GRID_LEVELS = int(os.getenv("GRID_LEVELS", "3"))
GRID_SIZE_USD = Decimal(os.getenv("GRID_SIZE_USD", "25"))
GRID_MIN_PRICE = Decimal(os.getenv("GRID_MIN_PRICE", "0"))
GRID_MAX_PRICE = Decimal(os.getenv("GRID_MAX_PRICE", "0"))
REFRESH_INTERVAL_SEC = float(os.getenv("GRID_REFRESH_SEC", "5"))


@dataclass
class Slot:
    """Track one outstanding order at a grid level."""

    external_id: Optional[str]
    price: Optional[Decimal]
    side: OrderSide


class GridTrader:
    """Very small grid trading helper.

    The trader keeps ``GRID_LEVELS`` buy orders below the mid price and the same
    number of sell orders above it.  Orders are sized in USD notional and
    converted to synthetic amount using the market's trading configuration.
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

        self._buy_slots: List[Slot] = [
            Slot(None, None, OrderSide.BUY) for _ in range(level_count)
        ]
        self._sell_slots: List[Slot] = [
            Slot(None, None, OrderSide.SELL) for _ in range(level_count)
        ]
        self._placement_lock = asyncio.Lock()

        # Ensure only one grid update runs at a time.
        # ``_next_mid`` keeps track of the most recent mid price requested while an
        # update is in progress so that redundant updates can be coalesced.
        self._update_lock = asyncio.Lock()
        self._next_mid: Decimal | None = None

        self._order_book: OrderBook | None = None
        self._market = None
        self._tick: Decimal | None = None
        self._best_bid: Decimal | None = None
        self._best_ask: Decimal | None = None
        self._closing = asyncio.Event()
        self._refresh_task: asyncio.Task | None = None
        self._grid_active = True

    # ------------------------------------------------------------------
    async def _queue_update(self, mid: Decimal) -> None:
        """Schedule a grid update for ``mid``.

        If an update is already running, only remember the most recent mid price and
        let the running update pick it up once finished.  This coalesces redundant
        updates when prices move quickly.
        """

        self._next_mid = mid
        if self._update_lock.locked():
            return
        while self._next_mid is not None:
            current = self._next_mid
            self._next_mid = None
            async with self._update_lock:
                await self._update_grid(current)

    async def _on_best_change(self, price: Decimal | None, *, is_bid: bool) -> None:
        if is_bid:
            self._best_bid = price
        else:
            self._best_ask = price
        if self._best_bid is not None and self._best_ask is not None:
            mid = (self._best_bid + self._best_ask) / 2
            await self._queue_update(mid)

    async def _create_order_book(self) -> OrderBook:
        return await OrderBook.create(
            self._endpoint_config,
            market_name=self._market.name,
            start=True,
            best_bid_change_callback=lambda bid: asyncio.create_task(
                self._on_best_change(bid.price if bid else None, is_bid=True)
            ),
            best_ask_change_callback=lambda ask: asyncio.create_task(
                self._on_best_change(ask.price if ask else None, is_bid=False)
            ),
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
        markets = await self.client.get_markets()
        if self.market_name not in markets:
            raise RuntimeError(f"Market {self.market_name} introuvable.")
        self._market = markets[self.market_name]
        self._tick = self.get_tick(self._market.trading_config)

        # Clear any stale orders from previous runs
        await call_with_retries(
            lambda: self.client.mass_cancel(markets=[self._market.name]),
            limiter=self._limiter,
        )

        self._order_book = await self._create_order_book()
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
            await self._order_book.close()
        await self.account.close()

    async def _refresh_loop(self) -> None:
        while not self._closing.is_set():
            if self._best_bid is not None and self._best_ask is not None:
                mid = (self._best_bid + self._best_ask) / 2
                await self._queue_update(mid)
            await asyncio.sleep(REFRESH_INTERVAL_SEC)

    # ------------------------------------------------------------------
    def _is_edit_not_found(self, e: Exception) -> bool:
        s = str(e)
        return "Edit order not found" in s or '"code":1142' in s or "not found" in s.lower()

    async def _ensure_order(
        self, slots: List[Slot], side: OrderSide, idx: int, price: Decimal
    ) -> None:
        slot = slots[idx]
        if slot.external_id and slot.price == price and slot.side == side:
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
                await call_with_retries(
                    lambda: _place(previous_id, price, side), limiter=self._limiter
                )
            slots[idx] = Slot(new_external_id, price, side)
        except Exception as e:
            if self._is_edit_not_found(e):
                new_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
                new_price = (
                    (price + self.grid_step)
                    if side == OrderSide.BUY
                    else (price - self.grid_step)
                )
                if self._tick is not None:
                    new_price = new_price.quantize(
                        self._tick,
                        rounding=ROUND_CEILING
                        if new_side == OrderSide.SELL
                        else ROUND_FLOOR,
                    )
                new_synthetic = _order_size(new_price)
                if new_synthetic >= min_size:
                    try:
                        async with self._placement_lock:
                            await call_with_retries(
                                lambda: _place(None, new_price, new_side),
                                limiter=self._limiter,
                            )
                        slots[idx] = Slot(new_external_id, new_price, new_side)
                        return
                    except Exception as e2:
                        logger.warning("order placement failed: %s", e2)
            else:
                logger.warning("order placement failed: %s", e)
            slots[idx] = Slot(None, None, side)

    async def _cancel_slot(self, slots: List[Slot], idx: int) -> None:
        slot = slots[idx]
        if not slot.external_id:
            slots[idx] = Slot(None, None, slot.side)
            return

        async def _cancel():
            return await self.client.cancel_order(order_external_id=slot.external_id)

        try:
            await call_with_retries(_cancel, limiter=self._limiter)
        except Exception as e:
            if not self._is_edit_not_found(e):
                logger.warning("order cancel failed: %s", e)
        slots[idx] = Slot(None, None, slot.side)

    async def _update_grid(self, mid: Decimal) -> None:
        if self._tick is None:
            return
        if mid < self.min_price or mid > self.max_price:
            if self._grid_active:
                logger.warning(
                    "mid price %s outside range [%s, %s]; grid inactive",
                    mid,
                    self.min_price,
                    self.max_price,
                )
                self._grid_active = False
            cancel_tasks = []
            for i in range(self.level_count):
                cancel_tasks.append(self._cancel_slot(self._buy_slots, i))
                cancel_tasks.append(self._cancel_slot(self._sell_slots, i))
            await asyncio.gather(*cancel_tasks)
            return
        elif not self._grid_active:
            logger.info(
                "mid price %s back inside range [%s, %s]; resuming grid",
                mid,
                self.min_price,
                self.max_price,
            )
            self._grid_active = True

        buy_tasks = []
        sell_tasks = []
        for i in range(self.level_count):
            level = i + 1
            buy_slot = self._buy_slots[i]
            if buy_slot.side == OrderSide.BUY:
                buy_px = (mid - self.grid_step * level).quantize(
                    self._tick, rounding=ROUND_FLOOR
                )
                if buy_px < self.min_price or buy_px > self.max_price:
                    buy_px = None
            else:
                buy_px = buy_slot.price
            if buy_px is not None and self.min_price <= buy_px <= self.max_price:
                buy_tasks.append(
                    self._ensure_order(self._buy_slots, buy_slot.side, i, buy_px)
                )
            else:
                buy_tasks.append(self._cancel_slot(self._buy_slots, i))

            sell_slot = self._sell_slots[i]
            if sell_slot.side == OrderSide.SELL:
                sell_px = (mid + self.grid_step * level).quantize(
                    self._tick, rounding=ROUND_CEILING
                )
                if sell_px < self.min_price or sell_px > self.max_price:
                    sell_px = None
            else:
                sell_px = sell_slot.price
            if sell_px is not None and self.min_price <= sell_px <= self.max_price:
                sell_tasks.append(
                    self._ensure_order(self._sell_slots, sell_slot.side, i, sell_px)
                )
            else:
                sell_tasks.append(self._cancel_slot(self._sell_slots, i))
        await asyncio.gather(*buy_tasks, *sell_tasks)


# ----------------------------------------------------------------------
async def main():
    account = TradingAccount()
    grid_step = (GRID_MAX_PRICE - GRID_MIN_PRICE) / (2 * GRID_LEVELS)
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
            pass

    await trader.start()
    print(f"[grid] started on {MARKET_NAME}")

    try:
        while not trader._closing.is_set():
            await asyncio.sleep(1.0)
    finally:
        await trader.stop()
        print("[grid] stopped.")


if __name__ == "__main__":
    asyncio.run(main())
