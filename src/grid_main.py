#!/usr/bin/env python3
"""
Grid Trading Strategy for Extended/X10 Perpetuals

This module implements an arithmetic grid trading strategy:
- Defines min_price, max_price, and number_of_grids
- Creates equally spaced grid levels between min and max
- Places buy orders below current price, sell orders above
- When price moves, triggers orders and flips grid levels
- Manages position and grid rebalancing
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Dict, List, Optional, Tuple

from x10.perpetual.orderbook import OrderBook
from x10.perpetual.orders import OrderSide
from x10.perpetual.simple_client.simple_trading_client import BlockingTradingClient

from account import TradingAccount
from rate_limit import build_rate_limiter
from backoff_utils import call_with_retries
from id_generator import uuid_external_id
from utils import logger, setup_logging, logging, _close_order_book


# --- Configuration ----------------------------------------------------------------------

# Market name can be provided via environment or interactively
MARKET_NAME = os.getenv("GRID_MARKET") or input("Market ? ")

# Grid parameters
GRID_MIN_PRICE = Decimal(os.getenv("GRID_MIN_PRICE", "0"))
GRID_MAX_PRICE = Decimal(os.getenv("GRID_MAX_PRICE", "0"))
GRID_LEVELS = int(os.getenv("GRID_LEVELS", "10"))
GRID_ORDER_USD = Decimal(os.getenv("GRID_ORDER_USD", "100"))

# Trading parameters
MAX_POSITION_USD = Decimal(os.getenv("GRID_MAX_POSITION_USD", "1000"))
REFRESH_INTERVAL_SEC = float(os.getenv("GRID_REFRESH_INTERVAL", "1.0"))
REBALANCE_THRESHOLD = Decimal(os.getenv("GRID_REBALANCE_THRESHOLD", "0.01"))

# Order management
MAX_ORDER_AGE_SEC = float(os.getenv("GRID_MAX_ORDER_AGE", "300"))


@dataclass
class GridLevel:
    """Represents a single grid level with order information."""
    price: Decimal
    order_id: Optional[str] = None
    order_side: Optional[OrderSide] = None
    created_at: float = 0.0
    filled: bool = False


class GridTrader:
    """Arithmetic grid trading strategy implementation."""

    def __init__(self, account: TradingAccount, market_name: str):
        self.account = account
        self.market_name = market_name
        self.client: BlockingTradingClient = account.get_blocking_client()
        self._endpoint_config = account.endpoint_config
        self._limiter = build_rate_limiter()

        self._order_book: Optional[OrderBook] = None
        self._market = None
        self._tick: Decimal | None = None
        self._closing = asyncio.Event()

        # Grid state
        self._grid_levels: List[GridLevel] = []
        self._current_price: Decimal = Decimal("0")
        self._last_rebalance_time: float = 0.0

        # Track active orders
        self._active_orders: Dict[str, GridLevel] = {}

    @staticmethod
    def get_tick(cfg) -> Decimal:
        """Get the minimum price change for the market."""
        min_change = getattr(cfg, "min_price_change", None)
        if min_change is not None:
            return Decimal(str(min_change))
        prec = getattr(cfg, "price_precision", 2)
        return Decimal(1).scaleb(-prec)

    def _calculate_grid_levels(self) -> List[Decimal]:
        """Calculate arithmetic grid levels between min and max prices."""
        if GRID_MIN_PRICE <= 0 or GRID_MAX_PRICE <= 0 or GRID_MIN_PRICE >= GRID_MAX_PRICE:
            raise ValueError("Invalid grid price range")

        if GRID_LEVELS < 2:
            raise ValueError("Grid levels must be at least 2")

        # Calculate arithmetic spacing
        price_range = GRID_MAX_PRICE - GRID_MIN_PRICE
        step = price_range / (GRID_LEVELS - 1)

        # Generate grid levels and round to market tick size
        levels = []
        for i in range(GRID_LEVELS):
            price = GRID_MIN_PRICE + step * i
            if self._tick is not None:
                price = price.quantize(self._tick, rounding=ROUND_CEILING)
            levels.append(price)

        return levels

    async def start(self) -> None:
        """Initialize market data and start grid trading."""
        try:
            # Get markets with timeout
            try:
                markets = await asyncio.wait_for(self.client.get_markets(), timeout=30.0)
            except asyncio.TimeoutError:
                raise RuntimeError("Timeout connecting to market data")
                
            if self.market_name not in markets:
                raise RuntimeError(f"Market {self.market_name} not found.")
            self._market = markets[self.market_name]
            self._tick = self.get_tick(self._market.trading_config)

            # Initialize order book
            self._order_book = await OrderBook.create(
                self._endpoint_config, market_name=self._market.name, start=True
            )

            # Calculate grid levels
            grid_prices = self._calculate_grid_levels()
            self._grid_levels = [GridLevel(price=price) for price in grid_prices]

            logger.info("Grid strategy initialized with %d levels", GRID_LEVELS)
            for i, level in enumerate(self._grid_levels):
                logger.info("Grid level %d: %s", i, level.price)

            # Start trading loop
            setup_logging(logging.INFO)
            self._trading_task = asyncio.create_task(self._trading_loop())
            
        except Exception as e:
            logger.exception("Failed to start grid strategy: %s", e)
            raise

    async def stop(self) -> None:
        """Gracefully stop trading and close resources."""
        self._closing.set()
        if self._trading_task:
            self._trading_task.cancel()
            try:
                await self._trading_task
            except asyncio.CancelledError:
                pass

        # Cancel all open orders
        await self._cancel_all_orders()

        if self._order_book:
            await _close_order_book(self._order_book)
        await self.account.close()

    async def _cancel_all_orders(self) -> None:
        """Cancel all open grid orders."""
        for order_id in list(self._active_orders.keys()):
            await self._safe_cancel(external_id=order_id)

    async def _safe_cancel(self, *, order_id: int | None = None, external_id: str | None = None) -> None:
        """Cancel an order while tolerating already-cancelled errors."""

        async def _op():
            if external_id is not None:
                return await self.client.cancel_order(order_external_id=external_id)
            return await self.client.cancel_order(order_id=order_id)

        try:
            await call_with_retries(_op, limiter=self._limiter)
        except Exception as e:
            msg = str(e).lower()
            if "not found" in msg:
                return
            logger.warning("Cancel failed: %s", e)

    async def _get_current_price(self) -> Decimal:
        """Get the current mid price from order book."""
        if not self._order_book:
            logger.warning("No order book available")
            return Decimal("0")

        bid_fn = getattr(self._order_book, "best_bid", None)
        bid = bid_fn() if bid_fn else None
        ask_fn = getattr(self._order_book, "best_ask", None)
        ask = ask_fn() if ask_fn else None

        if bid and ask:
            mid_price = (Decimal(bid.price) + Decimal(ask.price)) / 2
            logger.debug("Order book bid: %s, ask: %s, mid: %s", bid.price, ask.price, mid_price)
            return mid_price
        
        logger.warning("No valid bid/ask prices available")
        return Decimal("0")

    async def _get_signed_position(self) -> Decimal:
        """Return current position value (long positive, short negative)."""
        async_client = self.account.get_async_client()
        try:
            resp = await call_with_retries(
                lambda: async_client.account.get_positions(market_names=[self._market.name]),
                limiter=self._limiter,
            )
        except Exception as e:
            logger.warning("Position fetch failed: %s", e)
            return Decimal(0)

        positions = getattr(resp, "data", None) or []
        if not positions:
            return Decimal(0)
        pos = positions[0]
        value = Decimal(getattr(pos, "value", 0))
        side = getattr(pos, "side", "").lower()
        if side.startswith("long") or side.startswith("buy"):
            return value
        if side.startswith("short") or side.startswith("sell"):
            return -value
        return Decimal(0)

    def _find_closest_grid_level(self, price: Decimal) -> Tuple[int, GridLevel]:
        """Find the grid level closest to the current price."""
        if not self._grid_levels:
            raise ValueError("No grid levels available")
            
        closest_idx = 0
        closest_diff = abs(price - self._grid_levels[0].price)

        for i, level in enumerate(self._grid_levels):
            diff = abs(price - level.price)
            if diff < closest_diff:
                closest_diff = diff
                closest_idx = i

        return closest_idx, self._grid_levels[closest_idx]

    def _should_rebalance(self) -> bool:
        """Check if grid needs rebalancing based on price movement."""
        if not self._grid_levels:
            return False

        # Check if price moved significantly
        closest_idx, closest_level = self._find_closest_grid_level(self._current_price)
        price_diff = abs(self._current_price - closest_level.price) / closest_level.price
        
        # Check time-based rebalance
        time_since_rebalance = time.time() - self._last_rebalance_time
        
        return (price_diff > REBALANCE_THRESHOLD or 
                time_since_rebalance > MAX_ORDER_AGE_SEC)

    async def _check_order_fills(self) -> None:
        """Check which orders have been filled and update grid state."""
        if not self._market:
            return

        async_client = self.account.get_async_client()
        try:
            resp = await call_with_retries(
                lambda: async_client.account.get_open_orders(market_names=[self._market.name]),
                limiter=self._limiter,
            )
            open_order_ids = {o.external_id for o in (resp.data or []) if o.external_id}
        except Exception as e:
            logger.warning("Order check failed: %s", e)
            return

        # Update grid levels based on filled orders
        for order_id, grid_level in list(self._active_orders.items()):
            if order_id not in open_order_ids:
                # Order was filled or cancelled
                grid_level.filled = True
                grid_level.order_id = None
                grid_level.order_side = None
                del self._active_orders[order_id]

    async def _place_grid_order(self, level: GridLevel, side: OrderSide) -> None:
        """Place an order at a specific grid level."""
        if level.order_id or level.filled:
            return

        # Calculate order quantity
        qty = self._market.trading_config.calculate_order_size_from_value(
            GRID_ORDER_USD, level.price
        )
        
        # Check minimum order size
        min_size = self._market.trading_config.min_order_size
        if qty < min_size:
            logger.warning("Order size too small at level %s", level.price)
            return

        # Check position limits
        position = await self._get_signed_position()
        if (side == OrderSide.BUY and position >= MAX_POSITION_USD) or \
           (side == OrderSide.SELL and -position >= MAX_POSITION_USD):
            logger.debug("Position limit reached, skipping order")
            return

        # Generate external ID
        ext_id = uuid_external_id("grid", f"{side.name.lower()}_{level.price}")

        async def _op():
            return await self.client.create_and_place_order(
                market_name=self._market.name,
                amount_of_synthetic=qty,
                price=level.price,
                side=side,
                post_only=True,
                external_id=ext_id,
            )

        try:
            await call_with_retries(_op, limiter=self._limiter)
            level.order_id = ext_id
            level.order_side = side
            level.created_at = time.time()
            self._active_orders[ext_id] = level
            logger.info("Placed %s order at %s", side.name, level.price)
        except Exception as e:
            logger.warning("Failed to place %s order at %s: %s", side.name, level.price, e)

    async def _rebalance_grid(self) -> None:
        """Rebalance the grid based on current price."""
        logger.info("Rebalancing grid...")

        # Cancel all existing orders
        await self._cancel_all_orders()

        # Find current grid position
        closest_idx, _ = self._find_closest_grid_level(self._current_price)

        # Place buy orders below current price
        for i in range(closest_idx):
            level = self._grid_levels[i]
            if not level.filled:
                await self._place_grid_order(level, OrderSide.BUY)

        # Place sell orders above current price
        for i in range(closest_idx + 1, len(self._grid_levels)):
            level = self._grid_levels[i]
            if not level.filled:
                await self._place_grid_order(level, OrderSide.SELL)

        # The closest grid level remains empty
        self._last_rebalance_time = time.time()

    async def _handle_price_outside_grid(self) -> None:
        """Handle case when price moves outside grid boundaries."""
        if not self._grid_levels:
            return

        min_price = self._grid_levels[0].price
        max_price = self._grid_levels[-1].price

        if self._current_price < min_price:
            # Price below grid - all levels become SELL orders (expecting price to go lower)
            logger.info("Price below grid, converting all to sell orders")
            await self._cancel_all_orders()
            for level in self._grid_levels:
                if not level.filled:
                    await self._place_grid_order(level, OrderSide.SELL)

        elif self._current_price > max_price:
            # Price above grid - all levels become BUY orders (expecting price to go higher)
            logger.info("Price above grid, converting all to buy orders")
            await self._cancel_all_orders()
            for level in self._grid_levels:
                if not level.filled:
                    await self._place_grid_order(level, OrderSide.BUY)

    async def _trading_loop(self) -> None:
        """Main trading loop for grid strategy."""
        while not self._closing.is_set():
            try:
                # Get current market state
                self._current_price = await self._get_current_price()
                await self._check_order_fills()

                logger.info("Current price: %s, Active orders: %d", 
                           self._current_price, len(self._active_orders))

                # Skip trading if we don't have valid price data
                if self._current_price <= 0:
                    logger.warning("No valid price data available, skipping trading cycle")
                    await asyncio.sleep(REFRESH_INTERVAL_SEC)
                    continue

                # Handle price outside grid boundaries
                if self._grid_levels:
                    min_price = self._grid_levels[0].price
                    max_price = self._grid_levels[-1].price
                    
                    if self._current_price < min_price or self._current_price > max_price:
                        await self._handle_price_outside_grid()
                    elif self._should_rebalance():
                        await self._rebalance_grid()

                await asyncio.sleep(REFRESH_INTERVAL_SEC)

            except Exception as e:
                logger.exception("Trading loop error: %s", e)
                await asyncio.sleep(REFRESH_INTERVAL_SEC)


# --------------------------------------------------------------------------------------
async def amain() -> None:
    """Main entry point for grid trading strategy."""
    account = TradingAccount()
    trader = GridTrader(account=account, market_name=MARKET_NAME)
    
    try:
        await trader.start()
        
        # Wait for termination signal
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _signal_handler():
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                pass

        await stop_event.wait()
        
    finally:
        await trader.stop()


if __name__ == "__main__":
    asyncio.run(amain())
