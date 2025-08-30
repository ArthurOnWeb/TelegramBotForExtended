import os
import sys
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,expect_none",
    [
        (None, True),
        (SimpleNamespace(status="PLACED"), False),
    ],
)
async def test_ensure_order_handles_unexpected_responses(monkeypatch, response, expect_none):
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

    async def fake_create_and_place_order(**kwargs):
        return response

    trader.client = SimpleNamespace(create_and_place_order=fake_create_and_place_order)

    async def fake_call_with_retries(fn, limiter=None):
        return await fn()

    monkeypatch.setattr("grid_main.call_with_retries", fake_call_with_retries)

    await trader._ensure_order(trader._buy_slots, OrderSide.BUY, 0, Decimal("50"))

    slot = trader._buy_slots[0]
    if expect_none:
        assert slot.external_id is None
    else:
        assert slot.external_id is not None
