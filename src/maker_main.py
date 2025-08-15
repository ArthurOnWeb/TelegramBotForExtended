# maker_main.py
import asyncio
import os
import signal
import traceback
from pathlib import Path
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Optional, List

from x10.perpetual.orderbook import OrderBook
from x10.perpetual.orders import OrderSide
from x10.perpetual.simple_client.simple_trading_client import BlockingTradingClient

from account import TradingAccount
from rate_limit import build_rate_limiter
from backoff_utils import call_with_retries
from id_generator import SqliteExternalIdGenerator


# --- Paramètres de prod (surcouchables par variables d'env) ---
MARKET_NAME = os.getenv("MM_MARKET") or input("Market ? ")

# Path to persist external IDs using SQLite for multi-process safety
ID_DB_PATH = Path(__file__).with_name(f"external_ids_{MARKET_NAME}.db")

LEVELS_PER_SIDE = int(os.getenv("MM_LEVELS", "2"))        # nombre de quotes par côté
TARGET_ORDER_USD = Decimal(os.getenv("MM_TARGET_USD", "250"))
# Ecart relatif au meilleur prix : formule : best * (1 +/- (1+idx)/DIVISOR)
OFFSET_DIVISOR = Decimal(os.getenv("MM_OFFSET_DIVISOR", "400"))
# Limite d'envois concurrents (throttle)
MAX_IN_FLIGHT = int(os.getenv("MM_MAX_IN_FLIGHT", "4"))
# Coefficient de skew de taille d'ordre pour réduire l'exposition
EXPOSURE_SKEW = Decimal(os.getenv("MM_EXPOSURE_SKEW", "0"))


@dataclass
class Slot:
    external_id: Optional[str]
    price: Optional[Decimal]


class MarketMaker:
    def __init__(self, account: TradingAccount, market_name: str):
        self.account = account
        self.market_name = market_name
        self.client: BlockingTradingClient = account.get_blocking_client()
        self._endpoint_config = account.endpoint_config
        self._limiter = build_rate_limiter()

        self._buy_slots: List[Slot] = [Slot(None, None) for _ in range(LEVELS_PER_SIDE)]
        self._sell_slots: List[Slot] = [Slot(None, None) for _ in range(LEVELS_PER_SIDE)]
        self._pending_buy_job: Optional[asyncio.Future] = None
        self._pending_sell_job: Optional[asyncio.Future] = None
        self._throttle = asyncio.Semaphore(MAX_IN_FLIGHT)
        self._reconcile_task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None

        self._order_book: Optional[OrderBook] = None
        self._market = None
        self._closing = asyncio.Event()

        # Position cache for exposure calculations
        # Stores a tuple of (size, side, timestamp)
        self._pos_cache: tuple[Decimal, str, float] = (Decimal(0), "", 0.0)
        self._pos_ttl = 2.0  # seconds

        # External-id generator backed by SQLite
        self._id_generator = SqliteExternalIdGenerator(ID_DB_PATH)

        # Serialize order placement to avoid concurrent create/replace calls
        self._placement_lock = asyncio.Lock()

    async def _create_order_book(self) -> OrderBook:
        return await OrderBook.create(
            self._endpoint_config,
            market_name=self._market.name,
            start=True,
            best_ask_change_callback=lambda ask: asyncio.create_task(
                self._update_sell_orders(ask.price if ask else None)
            ),
            best_bid_change_callback=lambda bid: asyncio.create_task(
                self._update_buy_orders(bid.price if bid else None)
            ),
        )

    async def start(self):
        # Récupère infos marché une fois
        markets = await self.client.get_markets()
        if self.market_name not in markets:
            raise RuntimeError(f"Market {self.market_name} introuvable.")
        self._market = markets[self.market_name]

        # (optionnel) Clean ciblé au démarrage

        await call_with_retries(
            lambda: self.client.mass_cancel(markets=[self._market.name]),
            limiter=self._limiter,
        )

        # OrderBook (callbacks → on schedule des updates)
        self._order_book = await self._create_order_book()

        self._reconcile_task = asyncio.create_task(self._reconciler_loop(15.0))
        
    async def stop(self):
        self._closing.set()
        # Optionnel : cancel quotes à l’arrêt (à toi de décider)
        try:
            await call_with_retries(lambda: self.client.mass_cancel(markets=[self._market.name]),
                                    limiter=self._limiter)
        except Exception:
            pass

        if self._reconcile_task:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
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

    MM_PREFIX = "mm_"

    def _all_slots(self):
        return self._buy_slots + self._sell_slots

    async def _safe_cancel(self, *, order_id: Optional[int] = None, external_id: Optional[str] = None):
        async def _op():
            # BlockingTradingClient supports both signatures in SDK versions;
            # prefer external_id when present.
            if external_id is not None:
                return await self.client.cancel_order(order_external_id=external_id)
            return await self.client.cancel_order(order_id=order_id)

        try:
            await call_with_retries(_op, limiter=self._limiter)
        except Exception as e:
            s = str(e)
            # Treat “not found” as already gone
            if "Edit order not found" in s or '"code":1142' in s or "not found" in s.lower():
                return
            raise

    @staticmethod
    def get_tick(cfg) -> Decimal:
        tick=Decimal(str(getattr(cfg, "min_price_change", "0.001")))
        if tick!=None:
            return tick
        else:
            prec = getattr(cfg, "price_precision", 2)
            step = Decimal(1).scaleb(-prec)  # 10^-precision
            return step
        return
        
    # --- Position cache for exposure calculations ---
    async def _get_position_value(self) -> Decimal:
        value, side, ts = self._pos_cache
        now = asyncio.get_running_loop().time()
        if now - ts < self._pos_ttl:
            return value, side
        async_client = self.account.get_async_client()
        resp = await call_with_retries(
            lambda: async_client.account.get_positions(market_names=[self._market.name]),
            limiter=self._limiter,
        )
        positions = resp.data or []
        value = positions[0].value if positions else Decimal(0)
        side = positions[0].side if positions else ""
        self._pos_cache = (value, side, now)
        return value, side

    async def _apply_exposure_skew(
        self, base_amount: Decimal, side: OrderSide, price: Optional[Decimal] = None
    ) -> Decimal:
        """Adjust order size to reduce net exposure."""
        if not EXPOSURE_SKEW:
            return base_amount

        exposure, pos_side = await self._get_position_value()
        if exposure == 0:
            return base_amount

        reducing_exposure = (
            (pos_side == "LONG" and side == OrderSide.SELL)
            or (pos_side == "SHORT" and side == OrderSide.BUY)
        )
        if not reducing_exposure:
            return base_amount

        exposure_factor = (abs(exposure) / Decimal(50)) * EXPOSURE_SKEW
        multiplier = Decimal(1) + exposure_factor
        multiplier = max(Decimal(1), min(multiplier, Decimal(3)))

        adjusted = base_amount * multiplier
        return adjusted.quantize(base_amount)

    # ----------------- UPDATE LOOPS (callbacks) -----------------

    async def _update_sell_orders(self, best_ask: Optional[Decimal]):
        if self._closing.is_set():
            return
        if self._pending_sell_job and not self._pending_sell_job.done():
            # Un update est déjà en cours → on évite la tempête de tâches
            return

        coros = [
            self._ensure_slot(side=OrderSide.SELL, idx=i, best_px=best_ask)
            for i in range(LEVELS_PER_SIDE)
        ]
        self._pending_sell_job = asyncio.gather(*coros, return_exceptions=True)
        await self._pending_sell_job  # on attend pour séquencer les batchs

    async def _update_buy_orders(self, best_bid: Optional[Decimal]):
        if self._closing.is_set():
            return
        if self._pending_buy_job and not self._pending_buy_job.done():
            return

        coros = [
            self._ensure_slot(side=OrderSide.BUY, idx=i, best_px=best_bid)
            for i in range(LEVELS_PER_SIDE)
        ]
        self._pending_buy_job = asyncio.gather(*coros, return_exceptions=True)
        await self._pending_buy_job

    @staticmethod
    def _is_edit_not_found(e: Exception) -> bool:
        s = str(e)
        return "Edit order not found" in s or '"code":1142' in s or "code\": 1142" in s

    async def reconcile(self):
        """
        Periodically reconcile local slots with server state.
        - clears local ghosts
        - cancels server orphans
        """
        if not self._market:
            return

        async with self._placement_lock:
            # 1) Fetch open orders from server for this market
            # Prefer the async trading client’s account module if your Blocking client
            # doesn’t expose get_open_orders. If yours does, feel free to use it directly.
            async_client = self.account.get_async_client()
            resp = await call_with_retries(
                lambda: async_client.account.get_open_orders(market_names=[self._market.name]),
                limiter=self._limiter,
            )
            open_orders = resp.data or []

            # Cancel any orders missing an external_id so they can be recreated
            for o in open_orders:
                if not o.external_id:
                    await self._safe_cancel(order_id=o.id, external_id=None)

            # 2) Keep only our MM orders
            server_mm = {o.external_id: o for o in open_orders if (o.external_id or "").startswith(self.MM_PREFIX)}

            # 3) Local ghosts -> clear slot
            for slots in (self._buy_slots, self._sell_slots):
                for i, slot in enumerate(slots):
                    if not slot.external_id:
                        continue
                    if slot.external_id not in server_mm:
                        # order disappeared (filled/cancelled/rejected); free the slot
                        slots[i] = Slot(None, None)

            # 4) Server orphans -> cancel
            local_exts = {s.external_id for s in self._all_slots() if s.external_id}
            orphans = [o for ext, o in server_mm.items() if ext not in local_exts]
            for o in orphans:
                await self._safe_cancel(order_id=o.id, external_id=o.external_id)

            # 5) (Optional) Drift check: if slot price ≠ server price, clear to force re-quote
            #    (edits are already happening in your _ensure_slot; this just prevents stale state)
            ext_to_slot = {s.external_id: s for s in self._all_slots() if s.external_id}
            for ext, o in server_mm.items():
                s = ext_to_slot.get(ext)
                if s and s.price is not None and o.price != s.price:
                    # let the next best bid/ask callback re-place
                    s.external_id = None
                    s.price = None

    async def _reconciler_loop(self, interval_sec: float = 15.0):
        while not self._closing.is_set():
            try:
                await self.reconcile()
            except Exception as e:
                # keep going; log if you have a logger
                print(f"[reconcile] error: {e}")
            await asyncio.sleep(interval_sec)

    

    # ----------------- CORE PLACEMENT LOGIC -----------------

    async def _ensure_slot(self, *, side: OrderSide, idx: int, best_px: Optional[Decimal]):
        """
        Calcule un prix ajusté par rapport au best bid/ask,
        arrondit au tick, et fait un replace si nécessaire.
        """
        
        if self._closing.is_set() or best_px is None:
            return

        slots = self._sell_slots if side == OrderSide.SELL else self._buy_slots
        slot = slots[idx]

        # offset in ticks based on idx
        tick = self.get_tick(self._market.trading_config)
        rel = (Decimal(1) + Decimal(idx)) / OFFSET_DIVISOR
        direction = Decimal(1 if side == OrderSide.SELL else -1)
        candidate = best_px * (Decimal(1) + direction * rel)
        
        adjusted_price = candidate.quantize(
        tick, rounding=(ROUND_CEILING if side == OrderSide.SELL else ROUND_FLOOR)
        )

        # Même prix → rien à faire
        if slot.external_id and slot.price == adjusted_price:
            return

        # Taille en synthétique selon une cible USD
        synthetic_amount = self._market.trading_config.calculate_order_size_from_value(
            TARGET_ORDER_USD, adjusted_price
        )
        synthetic_amount = await self._apply_exposure_skew(
            synthetic_amount, side, adjusted_price
        )

        # Important : respect du min size
        min_size = self._market.trading_config.min_order_size
        if synthetic_amount < min_size:
            # Ajuste TARGET_ORDER_USD dans l’env si besoin
            return

        # Nouveau external_id (remplacement via previous_order_external_id)
        new_external_id = self._id_generator.next_id(
            self.MM_PREFIX, side.name.lower(), idx
        )

        try:
            async with self._placement_lock:
                async with self._throttle:
                    await call_with_retries(
                        lambda: self.client.create_and_place_order(
                            market_name=self._market.name,
                            amount_of_synthetic=synthetic_amount,
                            price=adjusted_price,
                            side=side,
                            post_only=True,
                            previous_order_external_id=slot.external_id,
                            external_id=new_external_id,
                        ),
                        limiter=self._limiter,
                    )

            # ✅ success → update slot
            slots[idx] = Slot(external_id=new_external_id, price=adjusted_price)

        except Exception as e:
            # Workaround SDK bug – don’t let this crash the loop
            if isinstance(e, RuntimeError) and "Lock is not acquired" in str(e):
                # treat as failed attempt, fall-through to maybe fresh create
                pass
            elif self._is_edit_not_found(e):
                # 1142 → fresh create (no previous_order_external_id)
                try:
                    async with self._placement_lock:
                        async with self._throttle:
                            async def _fresh():
                                return await self.client.create_and_place_order(
                                    market_name=self._market.name,
                                    amount_of_synthetic=synthetic_amount,
                                    price=adjusted_price,
                                    side=side,
                                    post_only=True,
                                    previous_order_external_id=None,     # ← important
                                    external_id=new_external_id,
                                )
                            resp = await call_with_retries(_fresh, limiter=self._limiter)
                    slots[idx] = Slot(external_id=new_external_id, price=adjusted_price)
                    return
                except Exception as e2:
                    print("Fresh create failed:\n", traceback.format_exc())
                    # Clear the slot so next tick won’t keep trying to replace a ghost
                    slots[idx] = Slot(external_id=None, price=None)
                return
            else:
                print("Place/replace failed:\n", traceback.format_exc())
                # If we failed *and* we tried to replace a non-existent order,
                # don’t keep the stale external_id around:
                if slot.external_id:
                    slots[idx] = Slot(external_id=None, price=None)
# ----------------- runner -----------------


async def main():
    account = TradingAccount()

    maker = MarketMaker(account=account, market_name=MARKET_NAME)

    # Gestion des signaux pour un arrêt propre
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(maker.stop()))
        except NotImplementedError:
            # Windows / environnements restreints
            pass

    await maker.start()
    print(f"[maker] started on {MARKET_NAME}")

    # Idle loop tant qu’on n’a pas demandé l’arrêt
    try:
        while not maker._closing.is_set():
            await asyncio.sleep(1.0)
    finally:
        await maker.stop()
        print("[maker] stopped.")

if __name__ == "__main__":
    asyncio.run(main())

