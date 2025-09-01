import os
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

# Set env vars to avoid interactive prompts during import
os.environ.setdefault("GRID_MARKET", "TEST-USD")
os.environ.setdefault("GRID_LEVELS", "2")
os.environ.setdefault("GRID_MIN_PRICE", "1")
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
async def test_buy_fill_places_sell_order(monkeypatch):
    trader = GridTrader(
        account=StubAccount(),
        market_name="TEST-USD",
        grid_step=Decimal("0.605"),
        level_count=2,
        order_size_usd=Decimal("10"),
        lower_bound=Decimal("1"),
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
    trader._slots = [
        Slot("buy0", Decimal("1.6"), OrderSide.BUY),
        Slot("buy1", Decimal("2.2"), OrderSide.BUY),
    ]
    trader._buy_slots = trader._slots

    async def fake_cancel_slot(slots, idx):
        slots[idx] = Slot(None, slots[idx].price, slots[idx].side)

    monkeypatch.setattr(trader, "_cancel_slot", fake_cancel_slot)

    captured = {}

    async def fake_ensure_order(slots, side, idx, price):
        captured["side"] = side
        captured["idx"] = idx
        captured["price"] = price

    monkeypatch.setattr(trader, "_ensure_order", fake_ensure_order)

    await trader._on_fill(0, OrderSide.BUY)

    assert captured["side"] == OrderSide.SELL
    assert captured["idx"] == 1
    assert captured["price"] == Decimal("2.2")
    assert trader._slots[1].price == Decimal("2.2")
    assert trader._slots[1].side == OrderSide.SELL
