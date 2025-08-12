# maker_main.py
import asyncio
import os
import random
import signal
import traceback
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Optional, Tuple, List

from x10.perpetual.orderbook import OrderBook
from x10.perpetual.orders import OrderSide
from x10.perpetual.simple_client.simple_trading_client import BlockingTradingClient

from account import TradingAccount  # <-- ta classe existante


# --- Paramètres de prod (surcouchables par variables d'env) ---
MARKET_NAME = os.getenv("MM_MARKET", "BTC-USD")
LEVELS_PER_SIDE = int(os.getenv("MM_LEVELS", "3"))        # nombre de quotes par côté
TARGET_ORDER_USD = Decimal(os.getenv("MM_TARGET_USD", "50"))
# Ecart relatif au meilleur prix : formule : best * (1 +/- (1+idx)/DIVISOR)
OFFSET_DIVISOR = Decimal(os.getenv("MM_OFFSET_DIVISOR", "400"))
# Limite d'envois concurrents (throttle)
MAX_IN_FLIGHT = int(os.getenv("MM_MAX_IN_FLIGHT", "4"))


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

        self._buy_slots: List[Slot] = [Slot(None, None) for _ in range(LEVELS_PER_SIDE)]
        self._sell_slots: List[Slot] = [Slot(None, None) for _ in range(LEVELS_PER_SIDE)]
        self._pending_buy_job: Optional[asyncio.Future] = None
        self._pending_sell_job: Optional[asyncio.Future] = None
        self._throttle = asyncio.Semaphore(MAX_IN_FLIGHT)

        self._order_book: Optional[OrderBook] = None
        self._market = None
        self._closing = asyncio.Event()

    async def start(self):
        # Récupère infos marché une fois
        markets = await self.client.get_markets()
        if self.market_name not in markets:
            raise RuntimeError(f"Market {self.market_name} introuvable.")
        self._market = markets[self.market_name]

        # (optionnel) Clean ciblé au démarrage
        await self.client.mass_cancel(markets=[self._market.name])

        # OrderBook (callbacks → on schedule des updates)
        self._order_book = await OrderBook.create(
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

    async def stop(self):
        self._closing.set()
        # Optionnel : cancel quotes à l’arrêt (à toi de décider)
        try:
            await self.client.mass_cancel(markets=[self._market.name])
        except Exception:
            pass
        if self._order_book:
            await self._order_book.close()
        await self.account.close()

    # ----------------- UPDATE LOOPS (callbacks) -----------------

    async def _update_sell_orders(self, best_ask: Optional[Decimal]):
        if self._closing.is_set():
            return
        if self._pending_sell_job and not self._pending_sell_job.done():
            # Un update est déjà en cours → on évite la tempête de tâches
            return

        async def _task(i: int):
            await self._ensure_slot(side=OrderSide.SELL, idx=i, best_px=best_ask)

        self._pending_sell_job = asyncio.gather(*[asyncio.create_task(_task(i)) for i in range(LEVELS_PER_SIDE)],
                                                return_exceptions=True)
        await self._pending_sell_job  # on attend pour séquencer les batchs

    async def _update_buy_orders(self, best_bid: Optional[Decimal]):
        if self._closing.is_set():
            return
        if self._pending_buy_job and not self._pending_buy_job.done():
            return

        async def _task(i: int):
            await self._ensure_slot(side=OrderSide.BUY, idx=i, best_px=best_bid)

        self._pending_buy_job = asyncio.gather(*[asyncio.create_task(_task(i)) for i in range(LEVELS_PER_SIDE)],
                                               return_exceptions=True)
        await self._pending_buy_job

    # ----------------- CORE PLACEMENT LOGIC -----------------

    async def _ensure_slot(self, *, side: OrderSide, idx: int, best_px: Optional[Decimal]):
        """
        Calcule un prix ajusté par rapport au best bid/ask,
        arrondit au tick, et fait un replace si nécessaire.
        """
        slots = self._sell_slots if side == OrderSide.SELL else self._buy_slots
        slot = slots[idx]

        if best_px is None:
            return

        # Ecart relatif (ex: pour idx=0 → 1/DIV, idx=1 → 2/DIV, etc.)
        rel = (Decimal(1) + Decimal(idx)) / OFFSET_DIVISOR
        direction = Decimal(1 if side == OrderSide.SELL else -1)
        candidate = best_px * (Decimal(1) + direction * rel)

        # Arrondi au tick selon le côté
        rounding = ROUND_CEILING if side == OrderSide.SELL else ROUND_FLOOR
        adjusted_price = self._market.trading_config.round_price(candidate, rounding)

        # Même prix → rien à faire
        if slot.external_id and slot.price == adjusted_price:
            return

        # Taille en synthétique selon une cible USD
        synthetic_amount = self._market.trading_config.calculate_order_size_from_value(
            TARGET_ORDER_USD, adjusted_price
        )

        # Important : respect du min size
        min_size = self._market.trading_config.min_order_size
        if synthetic_amount < min_size:
            # Ajuste TARGET_ORDER_USD dans l’env si besoin
            return

        # Nouveau external_id (remplacement via previous_order_external_id)
        new_external_id = f"mm_{side.name.lower()}_{idx}_{random.randint(1, 10**18)}"

        try:
            async with self._throttle:
                resp = await self.client.create_and_place_order(
                    market_name=self._market.name,
                    amount_of_synthetic=synthetic_amount,
                    price=adjusted_price,
                    side=side,
                    post_only=True,
                    previous_order_external_id=slot.external_id,  # replace si existant
                    external_id=new_external_id,
                )
            # Si OK, on remplace le slot
            slots[idx] = Slot(external_id=new_external_id, price=adjusted_price)

        except Exception:
            # Log minimal pour prod (tu peux brancher sur un logger)
            print("Place/replace failed:\n", traceback.format_exc())

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
