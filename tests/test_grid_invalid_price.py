import os
import sys
import logging
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

# Set env vars to avoid interactive prompts during import
os.environ.setdefault("GRID_MARKET", "TEST-USD")
os.environ.setdefault("GRID_LEVELS", "1")
os.environ.setdefault("GRID_MIN_PRICE", "0")
os.environ.setdefault("GRID_MAX_PRICE", "100")

# Ensure src/ is importable
sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from grid_main import GridTrader, OrderSide  # noqa: E402


class StubAccount:
    endpoint_config = SimpleNamespace()

    def get_blocking_client(self):  # pragma: no cover - simple stub
        return SimpleNamespace()


class FakeInvalidPrice(Exception):
    def __init__(self, message="Invalid price value", code=1141):
        super().__init__(message)
        self.code = code


@pytest.mark.asyncio
async def test_invalid_price_adjusts_with_tick(monkeypatch):
    trader = GridTrader(
        account=StubAccount(),
        market_name="TEST-USD",
        grid_step=Decimal("1"),
        level_count=1,
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
    trader._tick = Decimal("0.1")

    calls = []

    async def fake_create_and_place_order(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise FakeInvalidPrice()
        return SimpleNamespace(data=SimpleNamespace(status="PLACED"), error=None)

    trader.client = SimpleNamespace(create_and_place_order=fake_create_and_place_order)

    async def fake_call_with_retries(fn, limiter=None):
        return await fn()

    monkeypatch.setattr("grid_main.call_with_retries", fake_call_with_retries)

    await trader._ensure_order(trader._buy_slots, OrderSide.BUY, 0, Decimal("50.03"))

    assert len(calls) == 2
    assert calls[1]["price"] == Decimal("50.0")
    slot = trader._buy_slots[0]
    assert slot.price == Decimal("50.0")
    assert slot.external_id is not None


@pytest.mark.asyncio
async def test_invalid_price_without_tick_clears_slot(monkeypatch, caplog):
    trader = GridTrader(
        account=StubAccount(),
        market_name="TEST-USD",
        grid_step=Decimal("1"),
        level_count=1,
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
    trader._tick = None

    calls = []

    async def fake_create_and_place_order(**kwargs):
        calls.append(kwargs)
        raise FakeInvalidPrice()

    trader.client = SimpleNamespace(create_and_place_order=fake_create_and_place_order)

    async def fake_call_with_retries(fn, limiter=None):
        return await fn()

    monkeypatch.setattr("grid_main.call_with_retries", fake_call_with_retries)

    caplog.set_level(logging.WARNING, logger="extended_bot")

    await trader._ensure_order(trader._buy_slots, OrderSide.BUY, 0, Decimal("50.03"))

    assert len(calls) == 1
    slot = trader._buy_slots[0]
    assert slot.external_id is None
    assert any("invalid price value" in rec.message for rec in caplog.records)
