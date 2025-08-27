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
GRID_STEP_USD = Decimal(os.getenv("GRID_STEP_USD", "5"))
GRID_SIZE_USD = Decimal(os.getenv("GRID_SIZE_USD", "25"))
REFRESH_INTERVAL_SEC = float(os.getenv("GRID_REFRESH_SEC", "5"))


@dataclass
class Slot:
    """Track one outstanding order at a grid level."""

    external_id: Optional[str]
    price: Optional[Decimal]


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
    ):
        self.account = account
        self.market_name = market_name
        self.grid_step = grid_step
        self.level_count = level_count
        self.order_size_usd = order_size_usd

        self.client: BlockingTradingClient = account.get_blocking_client()
        self._endpoint_config = account.endpoint_config
        self._limiter = build_rate_limiter()

        self._buy_slots: List[Slot] = [Slot(None, None) for _ in range(level_count)]
        self._sell_slots: List[Slot] = [Slot(None, None) for _ in range(level_count)]
        self._placement_lock = asyncio.Lock()

        self._order_book: OrderBook | None = None
        self._market = None
        self._tick: Decimal | None = None
        self._best_bid: Decimal | None = None
        self._best_ask: Decimal | None = None
        self._closing = asyncio.Event()
        self._refresh_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    async def _on_best_change(self, price: Decimal | None, *, is_bid: bool) -> None:
        if is_bid:
            self._best_bid = price
        else:
            self._best_ask = price
        if self._best_bid is not None and self._best_ask is not None:
            mid = (self._best_bid + self._best_ask) / 2
            await self._update_grid(mid)

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
                await self._update_grid(mid)
            await asyncio.sleep(REFRESH_INTERVAL_SEC)

    # ------------------------------------------------------------------
    def _is_edit_not_found(self, e: Exception) -> bool:
        s = str(e)
        return "Edit order not found" in s or '"code":1142' in s or "not found" in s.lower()

    async def _ensure_order(
        self, slots: List[Slot], side: OrderSide, idx: int, price: Decimal
    ) -> None:
        slot = slots[idx]
        if slot.external_id and slot.price == price:
            return

        synthetic = self._market.trading_config.calculate_order_size_from_value(
            self.order_size_usd, price
        )
        min_size = self._market.trading_config.min_order_size
        if synthetic < min_size:
            return

        new_external_id = uuid_external_id("grid", side.name.lower(), idx)
        previous_id = slot.external_id

        async def _place(prev_id: Optional[str]):
            return await self.client.create_and_place_order(
                market_name=self._market.name,
                amount_of_synthetic=synthetic,
                price=price,
                side=side,
                post_only=True,
                previous_order_external_id=prev_id,
                external_id=new_external_id,
            )

        try:
            async with self._placement_lock:
                await call_with_retries(lambda: _place(previous_id), limiter=self._limiter)
            slots[idx] = Slot(new_external_id, price)
        except Exception as e:
            if self._is_edit_not_found(e):
                try:
                    async with self._placement_lock:
                        await call_with_retries(lambda: _place(None), limiter=self._limiter)
                    slots[idx] = Slot(new_external_id, price)
                    return
                except Exception as e2:
                    logger.warning("order placement failed: %s", e2)
            else:
                logger.warning("order placement failed: %s", e)
            slots[idx] = Slot(None, None)

    async def _update_grid(self, mid: Decimal) -> None:
        if self._tick is None:
            return
        buy_tasks = []
        sell_tasks = []
        for i in range(self.level_count):
            level = i + 1
            buy_px = (mid - self.grid_step * level).quantize(
                self._tick, rounding=ROUND_FLOOR
            )
            sell_px = (mid + self.grid_step * level).quantize(
                self._tick, rounding=ROUND_CEILING
            )
            buy_tasks.append(
                self._ensure_order(self._buy_slots, OrderSide.BUY, i, buy_px)
            )
            sell_tasks.append(
                self._ensure_order(self._sell_slots, OrderSide.SELL, i, sell_px)
            )
        await asyncio.gather(*buy_tasks, *sell_tasks)


# ----------------------------------------------------------------------
async def main():
    account = TradingAccount()
    trader = GridTrader(
        account=account,
        market_name=MARKET_NAME,
        grid_step=GRID_STEP_USD,
        level_count=GRID_LEVELS,
        order_size_usd=GRID_SIZE_USD,
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
