import os
import sys
import logging
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
        return SimpleNamespace()

    async def close(self):  # pragma: no cover - simple stub
        pass


@pytest.mark.asyncio
async def test_refresh_loop_repopulates_missing_orders(monkeypatch):
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

    call_count = 0

    async def fake_update_grid():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            trader._slots = [
                Slot("b1", Decimal("1"), OrderSide.BUY),
                Slot(None, Decimal("2"), OrderSide.SELL),
                Slot(None, Decimal("3"), OrderSide.SELL),
            ]
        else:
            trader._slots = [
                Slot("b1", Decimal("1"), OrderSide.BUY),
                Slot("s1", Decimal("2"), OrderSide.SELL),
                Slot(None, Decimal("3"), OrderSide.SELL),
            ]
        trader._buy_slots = trader._slots

    monkeypatch.setattr(trader, "_update_grid", fake_update_grid)

    async def fake_sleep(_):
        trader._closing.set()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    warnings: list[str] = []

    def fake_warning(msg, *args, **kwargs):
        warnings.append(msg % args)

    monkeypatch.setattr("grid_main.logger.warning", fake_warning)

    await trader._refresh_loop()

    assert call_count >= 2
    active_buys = sum(1 for s in trader._slots if s.side == OrderSide.BUY and s.external_id)
    active_sells = sum(1 for s in trader._slots if s.side == OrderSide.SELL and s.external_id)
    expected_total = len(trader._slots) - 1
    assert active_buys + active_sells == expected_total
    assert any("below expected" in w for w in warnings)
