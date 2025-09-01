import os
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import asyncio
import pytest

# Avoid interactive prompts during import of grid_main
os.environ.setdefault("GRID_MARKET", "TEST-USD")
os.environ.setdefault("GRID_LEVELS", "3")
os.environ.setdefault("GRID_MIN_PRICE", "0")
os.environ.setdefault("GRID_MAX_PRICE", "100")

# Ensure src/ is importable
sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from grid_main import GridTrader, Slot, OrderSide  # noqa: E402


class StubAccount:
    endpoint_config = SimpleNamespace()

    def get_blocking_client(self):  # pragma: no cover - simple stub
        return SimpleNamespace()

    def get_async_client(self):  # pragma: no cover - simple stub
        async def get_open_orders(market_names=None):
            return SimpleNamespace(data=[])
        return SimpleNamespace(account=SimpleNamespace(get_open_orders=get_open_orders))

    async def close(self):  # pragma: no cover - simple stub
        pass


@pytest.mark.asyncio
async def test_refresh_loop_restores_sides_from_none(monkeypatch):
    trader = GridTrader(
        account=StubAccount(),
        market_name="TEST-USD",
        grid_step=Decimal("1"),
        level_count=3,
        order_size_usd=Decimal("10"),
        lower_bound=Decimal("0"),
        upper_bound=Decimal("100"),
    )
    trader._market = SimpleNamespace(
        name="TEST-USD",
        trading_config=SimpleNamespace(
            calculate_order_size_from_value=lambda value, price: value / price,
            min_order_size=Decimal("0"),
        ),
    )
    trader._tick = Decimal("1")
    trader._level_prices = [Decimal("1"), Decimal("2"), Decimal("3")]
    trader._slots = [
        Slot(None, Decimal("1"), None),
        Slot(None, Decimal("2"), None),
        Slot(None, Decimal("3"), None),
    ]
    trader._buy_slots = trader._slots

    async def fake_call_with_retries(op, *, limiter, **kwargs):
        return await op()

    monkeypatch.setattr("grid_main.call_with_retries", fake_call_with_retries)

    async def fake_ensure_order(slots, side, idx, price):
        slots[idx] = Slot(f"{side.name.lower()}{idx}", price, side)

    monkeypatch.setattr(trader, "_ensure_order", fake_ensure_order)

    async def fake_cancel_slot(slots, idx):
        slots[idx] = Slot(None, slots[idx].price, slots[idx].side)

    monkeypatch.setattr(trader, "_cancel_slot", fake_cancel_slot)

    async def fake_sleep(_):
        trader._closing.set()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await trader._refresh_loop()

    active_buys = sum(1 for s in trader._slots if s.side == OrderSide.BUY and s.external_id)
    active_sells = sum(1 for s in trader._slots if s.side == OrderSide.SELL and s.external_id)
    assert active_buys > 0 and active_sells > 0
