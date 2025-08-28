import os
import sys
import importlib
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure src/ is importable
sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

# Default market so maker_main doesn't prompt
os.environ.setdefault("MM_MARKET", "TEST-USD")

import maker_main as mm


def _reload(static: bool):
    os.environ["MM_STATIC_GRID"] = "1" if static else "0"
    return importlib.reload(mm)


class StubAccount:
    endpoint_config = SimpleNamespace()

    def __init__(self, placer):
        self._client = SimpleNamespace(create_and_place_order=placer)

    def get_blocking_client(self):  # pragma: no cover - simple stub
        return self._client

    def get_async_client(self):  # pragma: no cover - simple stub
        return SimpleNamespace()


@pytest.mark.asyncio
async def test_dynamic_grid_reprices(monkeypatch):
    mm_mod = _reload(static=False)

    placed = []

    async def fake_place(**kwargs):
        placed.append(kwargs["price"])

    acct = StubAccount(fake_place)
    maker = mm_mod.MarketMaker(account=acct, market_name="TEST-USD")
    maker._market = SimpleNamespace(
        name="TEST-USD",
        trading_config=SimpleNamespace(
            calculate_order_size_from_value=lambda value, price: Decimal("1"),
            min_order_size=Decimal("0.1"),
        ),
    )
    maker._tick = Decimal("0.1")

    async def fake_call(fn, limiter=None, **_):
        return await fn()

    monkeypatch.setattr(mm_mod, "call_with_retries", fake_call)

    await maker._ensure_slot(side=mm_mod.OrderSide.BUY, idx=0, best_px=Decimal("100"))
    await maker._ensure_slot(side=mm_mod.OrderSide.BUY, idx=0, best_px=Decimal("101"))

    rel = (Decimal(1) + Decimal(0)) / mm_mod.OFFSET_DIVISOR
    expected1 = (Decimal("100") * (Decimal(1) - rel)).quantize(Decimal("0.1"), rounding=ROUND_FLOOR)
    expected2 = (Decimal("101") * (Decimal(1) - rel)).quantize(Decimal("0.1"), rounding=ROUND_FLOOR)

    assert placed == [expected1, expected2]


@pytest.mark.asyncio
async def test_static_grid_uses_anchor(monkeypatch):
    mm_mod = _reload(static=True)

    placed = []

    async def fake_place(**kwargs):
        placed.append(kwargs["price"])

    acct = StubAccount(fake_place)
    maker = mm_mod.MarketMaker(account=acct, market_name="TEST-USD")
    maker._market = SimpleNamespace(
        name="TEST-USD",
        trading_config=SimpleNamespace(
            calculate_order_size_from_value=lambda value, price: Decimal("1"),
            min_order_size=Decimal("0.1"),
        ),
    )
    maker._tick = Decimal("0.1")

    async def fake_call(fn, limiter=None, **_):
        return await fn()

    monkeypatch.setattr(mm_mod, "call_with_retries", fake_call)

    await maker._ensure_slot(side=mm_mod.OrderSide.SELL, idx=0, best_px=Decimal("200"))
    anchor_price = (Decimal("200") * (Decimal(1) + (Decimal(1) / mm_mod.OFFSET_DIVISOR))).quantize(
        Decimal("0.1"), rounding=ROUND_CEILING
    )
    assert placed == [anchor_price]
    assert maker._sell_anchor == Decimal("200")

    # Changing best price should not place a new order
    await maker._ensure_slot(side=mm_mod.OrderSide.SELL, idx=0, best_px=Decimal("210"))
    assert placed == [anchor_price]
    assert maker._sell_anchor == Decimal("200")

    maker.reset_anchor()
    await maker._ensure_slot(side=mm_mod.OrderSide.SELL, idx=0, best_px=Decimal("210"))
    new_price = (Decimal("210") * (Decimal(1) + (Decimal(1) / mm_mod.OFFSET_DIVISOR))).quantize(
        Decimal("0.1"), rounding=ROUND_CEILING
    )
    assert placed == [anchor_price, new_price]
    assert maker._sell_anchor == Decimal("210")
