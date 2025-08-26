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
import time

from x10.perpetual.orderbook import OrderBook
from x10.perpetual.orders import OrderSide
from x10.perpetual.simple_client.simple_trading_client import BlockingTradingClient

from account import TradingAccount
from rate_limit import build_rate_limiter
from backoff_utils import call_with_retries
from id_generator import uuid_external_id
from utils import logger,setup_logging,logging
from regime.calibration import VolatilityCalibrator
from strategy.quoting import Quoter

# --- Configuration ----------------------------------------------------------------------

# Market name can be provided via environment or interactively
MARKET_NAME = os.getenv("HYB_MARKET") or input("Market ? ")

# Regime detection parameters
# ``DEFAULT_SIGMA_THRESHOLD`` is used as a fallback until enough observations are
# collected to compute a dynamic threshold.
DEFAULT_SIGMA_THRESHOLD = float(os.getenv("HYB_SIGMA_THRESHOLD", "0.0012"))  # 0.12 %
MIN_SPREAD = float(os.getenv("HYB_MIN_SPREAD", "0.0005"))
VOL_WINDOW_SEC = int(os.getenv("HYB_VOL_WINDOW_SEC", "300"))

# Internal loop pacing
REFRESH_INTERVAL_SEC = float(os.getenv("HYB_REFRESH_INTERVAL", "1"))

# Derived: number of mid prices to keep (sampled every REFRESH_INTERVAL_SEC)
PRICE_HISTORY_LEN = max(1, int(VOL_WINDOW_SEC / REFRESH_INTERVAL_SEC))

# --- Trading parameters --------------------------------------------------------------

# Size of passive maker orders in USD notional
MAKER_ORDER_USD = Decimal(os.getenv("HYB_MAKER_USD", "50"))
# Size of taker scalp orders in USD notional
SCALP_ORDER_USD = Decimal(os.getenv("HYB_SCALP_USD", "25"))
# Maximum absolute inventory allowed (USD value)
MAX_POSITION_USD = Decimal(os.getenv("HYB_MAX_POSITION_USD", "250"))
# Maker quoting offset from mid-price (relative, e.g. 0.0005 = 5 bps)
MAKER_SPREAD = Decimal(os.getenv("HYB_MAKER_SPREAD", "0.0005"))
# Reprice threshold for existing maker orders
REPRICE_BPS = Decimal(os.getenv("HYB_REPRICE_BPS", "0.0002"))
# Stop-loss and take-profit distance (fraction of entry price)
STOP_BPS = Decimal(os.getenv("HYB_STOP_BPS", "0.001"))
TARGET_BPS = Decimal(os.getenv("HYB_TARGET_BPS", "0.0015"))

# Default regression coefficients for quoting parameters.
# ``beta_k`` intercept matches ``MAKER_SPREAD`` so behaviour remains unchanged
# until a calibration routine provides better estimates.  ``beta_lambda`` is
# initialised so the returned lambda_ scales the order size by 1.0.
DEFAULT_BETA_K = [0.0, 0.0, float(MAKER_SPREAD)]
DEFAULT_BETA_LAMBDA = [0.0, 0.0, 1.0]


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
    volatility.  A :class:`~regime.calibration.VolatilityCalibrator` keeps track
    of these measurements to derive a dynamic calm/agitated threshold (20th
    percentile).  When volatility is below this threshold and spreads are not
    too tight, it engages in passive market making.  Otherwise it will try to
    capture short bursts of momentum using taker orders.
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

        # Volatility calibration
        self._calibrator = VolatilityCalibrator()

        # Quoting helper updating k and lambda parameters from market state
        self._quoter: Quoter | None = None
        self._k: float = float(MAKER_SPREAD)
        self._lambda_: float = 1.0

        # Track outstanding maker orders
        self._mm_buy_id: Optional[str] = None
        self._mm_buy_price: Optional[Decimal] = None
        self._mm_sell_id: Optional[str] = None
        self._mm_sell_price: Optional[Decimal] = None

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

        setup_logging(logging.INFO)
        # Instantiate quoter with current order book
        self._quoter = Quoter(
            self._order_book,
            DEFAULT_BETA_K,
            DEFAULT_BETA_LAMBDA,
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
            bid_fn = getattr(self._order_book, "best_bid", None)
            bid = bid_fn() if bid_fn else None
            ask_fn = getattr(self._order_book, "best_ask", None)
            ask = ask_fn() if ask_fn else None
            if bid and ask:
                mid = float((bid.price + ask.price) / 2)
                self._mid_history.append(mid)
                spread = float(ask.price - bid.price)
            else:
                spread = float("inf")

            sigma = self._realised_vol()
            if self._quoter:
                self._k, self._lambda_ = self._quoter.refresh_parameters(sigma)
            # Update calibration with latest 5min volatility estimate
            self._calibrator.update(self.market_name, sigma)
            threshold = self._calibrator.threshold(self.market_name)
            if threshold == 0.0:
                threshold = DEFAULT_SIGMA_THRESHOLD

            mode = "calm"
            if sigma > threshold or spread < MIN_SPREAD:
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

    async def _safe_cancel(self, *, order_id: int | None = None, external_id: str | None = None) -> None:
        """Cancel an order while tolerating already-cancelled errors."""

        async def _op():
            if external_id is not None:
                return await self.client.cancel_order(order_external_id=external_id)
            return await self.client.cancel_order(order_id=order_id)

        try:
            await call_with_retries(_op, limiter=self._limiter)
        except Exception as e:  # noqa: PERF203 - broad catch to log
            msg = str(e).lower()
            if "not found" in msg:
                return
            logger.warning("cancel failed: %s", e)

    async def _get_signed_position(self) -> Decimal:
        """Return current position value (long positive, short negative)."""

        async_client = self.account.get_async_client()
        try:
            resp = await call_with_retries(
                lambda: async_client.account.get_positions(market_names=[self._market.name]),
                limiter=self._limiter,
            )
        except Exception as e:  # noqa: PERF203
            logger.warning("position fetch failed: %s", e)
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

    async def _check_mm_fills(self) -> None:
        """Update state of passive maker orders if filled or cancelled."""

        if not self._market:
            return

        if not (self._mm_buy_id or self._mm_sell_id):
            return

        async_client = self.account.get_async_client()

        try:
            resp = await call_with_retries(
                lambda: async_client.account.get_open_orders(market_names=[self._market.name]),
                limiter=self._limiter,
            )
            open_ids = {o.external_id for o in (resp.data or [])}
        except Exception as e:  # noqa: PERF203 - broad catch to log
            logger.warning("[MM] check orders failed: %s", e)
            return

        if self._mm_buy_id and self._mm_buy_id not in open_ids:
            self._mm_buy_id = None
            self._mm_buy_price = None

        if self._mm_sell_id and self._mm_sell_id not in open_ids:
            self._mm_sell_id = None
            self._mm_sell_price = None

    # ------------------------------------------------------------------
    async def _trading_loop(self) -> None:
        """Dispatch to the appropriate mode's trading function."""
        while not self._closing.is_set():
            try:
                await self._check_mm_fills()
                distance = self._distance_to_entry()
                logger.info("mode=%s distance_to_entry=%.5f", self._mode, distance)
                if self._mode == "calm":
                    await self._market_make()
                else:
                    await self._scalp_momentum()
                await asyncio.sleep(REFRESH_INTERVAL_SEC)
            except Exception:  # noqa: BLE001
                logger.exception("trading loop aborted")
                await asyncio.sleep(REFRESH_INTERVAL_SEC)

    def _distance_to_entry(self) -> float:
        """Return relative distance of mid price from VWAP for entry."""

        if not self._mid_history:
            return 0.0
        mid = self._mid_history[-1]
        vwap = sum(self._mid_history) / len(self._mid_history)
        if vwap == 0:
            return 0.0
        return mid / vwap - 1

    async def _market_make(self) -> None:
        """Place passive quotes while respecting inventory limits."""

        await self._check_mm_fills()

        bid_fn = getattr(self._order_book, "best_bid", None)
        bid = bid_fn() if bid_fn else None
        ask_fn = getattr(self._order_book, "best_ask", None)
        ask = ask_fn() if ask_fn else None
        if not (bid and ask):
            return

        mid = (Decimal(bid.price) + Decimal(ask.price)) / 2
        pos = await self._get_signed_position()

        buy_allowed = pos < MAX_POSITION_USD
        sell_allowed = -pos < MAX_POSITION_USD

        # Cancel sides that would exceed inventory
        if not buy_allowed and self._mm_buy_id:
            await self._safe_cancel(external_id=self._mm_buy_id)
            self._mm_buy_id = None
            self._mm_buy_price = None
        if not sell_allowed and self._mm_sell_id:
            await self._safe_cancel(external_id=self._mm_sell_id)
            self._mm_sell_id = None
            self._mm_sell_price = None

        size_usd = MAKER_ORDER_USD * Decimal(self._lambda_) if self._lambda_ else MAKER_ORDER_USD
        qty = size_usd / mid if mid else Decimal(0)

        if buy_allowed:
            spread = Decimal(self._k) if self._k else MAKER_SPREAD
            price = mid * (1 - spread)
            # Reprice if moved
            if self._mm_buy_id and self._mm_buy_price:
                diff = abs(price - self._mm_buy_price) / self._mm_buy_price
                if diff > REPRICE_BPS:
                    await self._safe_cancel(external_id=self._mm_buy_id)
                    self._mm_buy_id = None
                    self._mm_buy_price = None
            if self._mm_buy_id is None:
                ext_id = uuid_external_id("hyb_mm", "buy")
                async def _op():
                    return await self.client.create_and_place_order(
                        market_name=self._market.name,
                        side=OrderSide.BUY,
                        order_type="limit",
                        size=qty,
                        price=price,
                        time_in_force="GTC",
                        external_id=ext_id,
                    )
                try:
                    await call_with_retries(_op, limiter=self._limiter)
                    self._mm_buy_id = ext_id
                    self._mm_buy_price = price
                except Exception as e:  # noqa: PERF203
                    logger.warning("[MM] buy order failed: %s", e)

        if sell_allowed:
            spread = Decimal(self._k) if self._k else MAKER_SPREAD
            price = mid * (1 + spread)
            if self._mm_sell_id and self._mm_sell_price:
                diff = abs(price - self._mm_sell_price) / self._mm_sell_price
                if diff > REPRICE_BPS:
                    await self._safe_cancel(external_id=self._mm_sell_id)
                    self._mm_sell_id = None
                    self._mm_sell_price = None
            if self._mm_sell_id is None:
                ext_id = uuid_external_id("hyb_mm", "sell")
                async def _op():
                    return await self.client.create_and_place_order(
                        market_name=self._market.name,
                        side=OrderSide.SELL,
                        order_type="limit",
                        size=qty,
                        price=price,
                        time_in_force="GTC",
                        external_id=ext_id,
                    )
                try:
                    await call_with_retries(_op, limiter=self._limiter)
                    self._mm_sell_id = ext_id
                    self._mm_sell_price = price
                except Exception as e:  # noqa: PERF203
                    logger.warning("[MM] sell order failed: %s", e)

    async def _scalp_momentum(self) -> None:
        """Execute momentum trades with stop and target management."""

        bid_fn = getattr(self._order_book, "best_bid", None)
        bid = bid_fn() if bid_fn else None
        ask_fn = getattr(self._order_book, "best_ask", None)
        ask = ask_fn() if ask_fn else None
        if not (bid and ask):
            return

        mid = (Decimal(bid.price) + Decimal(ask.price)) / 2

        # Manage open trade first
        if self._open_trade:
            trade = self._open_trade
            if trade.side == OrderSide.BUY and bid:
                price = Decimal(bid.price)
                if price <= trade.stop or price >= trade.target:
                    await self._close_trade(trade)
            elif trade.side == OrderSide.SELL and ask:
                price = Decimal(ask.price)
                if price >= trade.stop or price <= trade.target:
                    await self._close_trade(trade)
            return

        vwap = (
            sum(self._mid_history) / len(self._mid_history)
            if self._mid_history
            else float(mid)
        )

        side: OrderSide | None = None
        if mid > vwap:
            side = OrderSide.BUY
        elif mid < vwap:
            side = OrderSide.SELL
        else:
            return

        pos = await self._get_signed_position()
        if side == OrderSide.BUY and pos >= MAX_POSITION_USD:
            return
        if side == OrderSide.SELL and -pos >= MAX_POSITION_USD:
            return

        qty = SCALP_ORDER_USD / mid if mid else Decimal(0)
        ext_id = uuid_external_id("hyb_scalp", side.name.lower())

        async def _op():
            return await self.client.create_and_place_order(
                market_name=self._market.name,
                side=side,
                order_type="market",
                size=qty,
                external_id=ext_id,
            )

        try:
            await call_with_retries(_op, limiter=self._limiter)
        except Exception as e:  # noqa: PERF203
            logger.warning("[SCALP] open trade failed: %s", e)
            return

        if side == OrderSide.BUY:
            stop = mid * (1 - STOP_BPS)
            target = mid * (1 + TARGET_BPS)
        else:
            stop = mid * (1 + STOP_BPS)
            target = mid * (1 - TARGET_BPS)

        self._open_trade = Trade(
            side=side,
            entry=mid,
            stop=stop,
            target=target,
            opened_at=time.time(),
        )

    async def _close_trade(self, trade: Trade) -> None:
        """Close an open trade with a market order."""

        exit_side = OrderSide.SELL if trade.side == OrderSide.BUY else OrderSide.BUY
        qty = SCALP_ORDER_USD / trade.entry if trade.entry else Decimal(0)
        ext_id = uuid_external_id("hyb_exit", exit_side.name.lower())

        async def _op():
            return await self.client.create_and_place_order(
                market_name=self._market.name,
                side=exit_side,
                order_type="market",
                size=qty,
                external_id=ext_id,
            )

        try:
            await call_with_retries(_op, limiter=self._limiter)
        except Exception as e:  # noqa: PERF203
            logger.warning("[SCALP] failed to exit trade: %s", e)
            return

        self._open_trade = None


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
