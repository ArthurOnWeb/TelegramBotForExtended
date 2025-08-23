# hybrid_main.py
"""Hybrid trading entry point combining market making and momentum scalping.

This module implements a simple regime-switching strategy for Extended / X10
perpetuals.  When short-term volatility is low and spreads are wide, it quotes
passive orders on both sides of the book (market making).  When volatility
spikes or the spread tightens, it temporarily switches to an aggressive
momentum scalping mode.

The implementation mirrors the structure of :mod:`maker_main` but focuses on
clarity rather than performance.  The core trading logic (placement,
cancellation, PnL management) is intentionally left as placeholders so the file
can serve as a starting point for further development.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Deque, Optional

from x10.perpetual.orderbook import OrderBook
from x10.perpetual.orders import OrderSide
from x10.perpetual.simple_client.simple_trading_client import BlockingTradingClient

from account import TradingAccount
from rate_limit import build_rate_limiter
from utils import logger

# --- Configuration ----------------------------------------------------------------------

# Market name can be provided via environment or interactively
MARKET_NAME = os.getenv("HYB_MARKET") or input("Market ? ")

# Regime detection parameters
SIGMA_THRESHOLD = float(os.getenv("HYB_SIGMA_THRESHOLD", "0.0012"))  # 0.12 %
MIN_SPREAD = float(os.getenv("HYB_MIN_SPREAD", "0.0005"))
VOL_WINDOW_SEC = int(os.getenv("HYB_VOL_WINDOW_SEC", "300"))

# Internal loop pacing
REFRESH_INTERVAL_SEC = float(os.getenv("HYB_REFRESH_INTERVAL", "1"))

# Derived: number of mid prices to keep (sampled every REFRESH_INTERVAL_SEC)
PRICE_HISTORY_LEN = max(1, int(VOL_WINDOW_SEC / REFRESH_INTERVAL_SEC))


@dataclass
class Trade:
    """A very small structure holding details about an open momentum trade."""

    side: OrderSide
    entry: Decimal
    stop: Decimal
    target: Decimal
    opened_at: float


class HybridTrader:
    """Regime-switching trading bot.

    The trader maintains a short history of mid-prices to estimate realised
    volatility.  When volatility is below ``SIGMA_THRESHOLD`` and spreads are
    not too tight, it engages in passive market making.  Otherwise it will
    try to capture short bursts of momentum using taker orders.
    """

    def __init__(self, account: TradingAccount, market_name: str):
        self.account = account
        self.market_name = market_name
        self.client: BlockingTradingClient = account.get_blocking_client()
        self._endpoint_config = account.endpoint_config
        self._limiter = build_rate_limiter()

        self._order_book: Optional[OrderBook] = None
        self._market = None
        self._closing = asyncio.Event()

        self._mid_history: Deque[float] = deque(maxlen=PRICE_HISTORY_LEN)
        self._mode: str = "calm"
        self._open_trade: Optional[Trade] = None

        self._regime_task: asyncio.Task | None = None
        self._trade_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Initialise market data and background tasks."""
        markets = await self.client.get_markets()
        if self.market_name not in markets:
            raise RuntimeError(f"Market {self.market_name} introuvable.")
        self._market = markets[self.market_name]

        # Order book used for both quoting and regime detection
        self._order_book = await OrderBook.create(
            self._endpoint_config, market_name=self._market.name, start=True
        )

        self._regime_task = asyncio.create_task(self._regime_loop())
        self._trade_task = asyncio.create_task(self._trading_loop())

    async def stop(self) -> None:
        """Gracefully stop all background tasks and close resources."""
        self._closing.set()
        for task in (self._regime_task, self._trade_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._order_book:
            await self._order_book.close()
        await self.account.close()

    # ------------------------------------------------------------------
    async def _regime_loop(self) -> None:
        """Continuously evaluate market regime based on recent volatility."""
        while not self._closing.is_set():
            bid = getattr(self._order_book, "best_bid", None)
            ask = getattr(self._order_book, "best_ask", None)
            if bid and ask:
                mid = float((bid.price + ask.price) / 2)
                self._mid_history.append(mid)
                spread = float(ask.price - bid.price)
            else:
                spread = float("inf")

            sigma = self._realised_vol()
            mode = "calm"
            if sigma > SIGMA_THRESHOLD or spread < MIN_SPREAD:
                mode = "agitated"
            if mode != self._mode:
                logger.info(
                    "Switching regime %s → %s (sigma=%.5f, spread=%.5f)",
                    self._mode,
                    mode,
                    sigma,
                    spread,
                )
                self._mode = mode
            await asyncio.sleep(REFRESH_INTERVAL_SEC)

    def _realised_vol(self) -> float:
        """Compute simple realised volatility of stored mid prices."""
        if len(self._mid_history) < 2:
            return 0.0
        rets = [
            self._mid_history[i] / self._mid_history[i - 1] - 1
            for i in range(1, len(self._mid_history))
        ]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        return var ** 0.5

    # ------------------------------------------------------------------
    async def _trading_loop(self) -> None:
        """Dispatch to the appropriate mode's trading function."""
        while not self._closing.is_set():
            if self._mode == "calm":
                await self._market_make()
            else:
                await self._scalp_momentum()
            await asyncio.sleep(REFRESH_INTERVAL_SEC)

    async def _market_make(self) -> None:
        """Very small placeholder for passive quoting logic."""
        bid = getattr(self._order_book, "best_bid", None)
        ask = getattr(self._order_book, "best_ask", None)
        if not (bid and ask):
            return
        mid = (Decimal(bid.price) + Decimal(ask.price)) / 2
        logger.debug("[MM] quoting around mid %s", mid)
        # Actual order placement/cancellation should be implemented here.

    async def _scalp_momentum(self) -> None:
        """Placeholder for taker-style momentum scalping."""
        if self._open_trade:
            # Manage existing trade (e.g. trailing stop); omitted for brevity
            return

        bid = getattr(self._order_book, "best_bid", None)
        ask = getattr(self._order_book, "best_ask", None)
        if not (bid and ask):
            return
        mid = (Decimal(bid.price) + Decimal(ask.price)) / 2
        vwap = (
            sum(self._mid_history) / len(self._mid_history)
            if self._mid_history
            else float(mid)
        )
        if mid > vwap:
            logger.debug("[SCALP] breakout long at %s", mid)
        elif mid < vwap:
            logger.debug("[SCALP] breakout short at %s", mid)
        # Real order submission and risk management to be added.


# ----------------------------------------------------------------------------------------
async def amain() -> None:
    account = TradingAccount()
    trader = HybridTrader(account=account, market_name=MARKET_NAME)
    await trader.start()

    # Wait until externally stopped (CTRL+C)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal_handler():
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Signal handlers aren't available on some platforms (e.g. Windows)
            pass

    await stop_event.wait()
    await trader.stop()


if __name__ == "__main__":  # pragma: no cover - manual execution only
    asyncio.run(amain())
