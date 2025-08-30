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

from grid_main import GridTrader, Slot, OrderSide  # noqa: E402


class StubAccount:
    endpoint_config = SimpleNamespace()

    def get_blocking_client(self):  # pragma: no cover - simple stub
        return SimpleNamespace()

    def get_async_client(self):  # pragma: no cover - simple stub
        return SimpleNamespace(account=SimpleNamespace())


@pytest.mark.asyncio
async def test_failed_replace_cancels_previous(monkeypatch):
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
    trader._slots[0] = Slot("prev", Decimal("50"), OrderSide.SELL)
    trader._buy_slots = trader._slots

    cancel_calls = []

    async def fake_create_and_place_order(**kwargs):
        return SimpleNamespace(data=SimpleNamespace(status="REJECTED"), error=None)

    async def fake_cancel_order(**kwargs):
        cancel_calls.append(kwargs)

    trader.client = SimpleNamespace(
        create_and_place_order=fake_create_and_place_order,
        cancel_order=fake_cancel_order,
    )

    async def fake_call_with_retries(fn, limiter=None):
        return await fn()

    monkeypatch.setattr("grid_main.call_with_retries", fake_call_with_retries)

    await trader._ensure_order(trader._slots, OrderSide.BUY, 0, Decimal("50"))

    assert any(call.get("order_external_id") == "prev" for call in cancel_calls)
    assert trader._slots[0].external_id is None
    assert trader._slots[0].side == OrderSide.BUY


@pytest.mark.asyncio
async def test_update_grid_cancels_orphans(monkeypatch, caplog):
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
    trader._tick = Decimal("1")
    trader._slots = [Slot("tracked", Decimal("10"), OrderSide.BUY)]
    trader._buy_slots = trader._slots

    tracked_order = SimpleNamespace(external_id="tracked", id=1, price=Decimal("10"), side=OrderSide.BUY)
    orphan_order = SimpleNamespace(external_id="orphan", id=2, price=Decimal("11"), side=OrderSide.SELL)
    no_ext_order = SimpleNamespace(external_id=None, id=3, price=Decimal("12"), side=OrderSide.BUY)

    async def fake_get_open_orders(market_names=None):
        return SimpleNamespace(data=[tracked_order, orphan_order, no_ext_order])

    trader.account.get_async_client = lambda: SimpleNamespace(account=SimpleNamespace(get_open_orders=fake_get_open_orders))

    cancel_calls = []

    async def fake_cancel_order(**kwargs):
        cancel_calls.append(kwargs)

    trader.client = SimpleNamespace(cancel_order=fake_cancel_order)

    async def fake_call_with_retries(fn, limiter=None):
        return await fn()

    monkeypatch.setattr("grid_main.call_with_retries", fake_call_with_retries)

    async def fake_ensure_order(*args, **kwargs):
        return None

    async def fake_cancel_slot(slots, idx):
        slots[idx] = Slot(None, slots[idx].price, slots[idx].side)

    monkeypatch.setattr(trader, "_ensure_order", fake_ensure_order)
    monkeypatch.setattr(trader, "_cancel_slot", fake_cancel_slot)

    caplog.set_level(logging.WARNING, logger="extended_bot")
    await trader._update_grid()

    assert any(c.get("order_external_id") == "orphan" for c in cancel_calls)
    assert any(c.get("order_id") == 3 for c in cancel_calls)
    assert any("reconcile cancel orphan order" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_update_grid_logs_fetch_failure(monkeypatch, caplog):
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
    trader._tick = Decimal("1")
    trader._slots = [Slot("tracked", Decimal("10"), OrderSide.BUY)]
    trader._buy_slots = trader._slots

    async def fake_get_open_orders(market_names=None):
        raise RuntimeError("boom")

    trader.account.get_async_client = lambda: SimpleNamespace(
        account=SimpleNamespace(get_open_orders=fake_get_open_orders)
    )

    cancel_calls = []

    async def fake_cancel_order(**kwargs):
        cancel_calls.append(kwargs)

    trader.client = SimpleNamespace(cancel_order=fake_cancel_order)

    async def fake_call_with_retries(fn, limiter=None):
        return await fn()

    monkeypatch.setattr("grid_main.call_with_retries", fake_call_with_retries)

    caplog.set_level(logging.ERROR, logger="extended_bot")

    await trader._update_grid()

    assert cancel_calls == []
    assert any(
        "reconcile fetch open orders failed" in rec.message for rec in caplog.records
    )
