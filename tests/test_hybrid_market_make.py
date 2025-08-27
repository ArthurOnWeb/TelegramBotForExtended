import os
import sys
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure src/ is importable
sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("HYB_MARKET", "TEST-USD")
from hybrid_main import HybridTrader, MAX_POSITION_USD  # noqa: E402


class StubAccount:
    """Minimal account stub for constructing :class:`HybridTrader`."""

    endpoint_config = SimpleNamespace()

    def get_blocking_client(self):  # pragma: no cover - simple stub
        return SimpleNamespace()

    def get_async_client(self):  # pragma: no cover - simple stub
        return SimpleNamespace(
            account=SimpleNamespace(get_positions=lambda market_names: SimpleNamespace(data=[]))
        )


def _mk_trader():
    trader = HybridTrader(account=StubAccount(), market_name="TEST-USD")
    trader._market = SimpleNamespace(
        name="TEST-USD",
        trading_config=SimpleNamespace(
            calculate_order_size_from_value=lambda value, price: value / price
        ),
    )
    trader._order_book = SimpleNamespace(
        best_bid=lambda: SimpleNamespace(price=100, amount=1),
        best_ask=lambda: SimpleNamespace(price=101, amount=1),
    )
    trader._tick = None
    trader._sigma = 0.01
    trader._lambda_ = 1.0
    trader._mm_levels = 1
    trader._mm_max_age = 10
    return trader


@pytest.mark.asyncio
async def test_market_make_reprices_stale_buy(monkeypatch):
    trader = _mk_trader()

    now = time.time()
    trader._mm_buy_orders = {0: ("b1", Decimal("99"), now - 20)}
    trader._mm_buy_id = "b1"
    trader._mm_sell_orders = {}

    cancels = []

    async def fake_cancel(external_id):
        cancels.append(external_id)

    trader._safe_cancel = fake_cancel

    placements = []

    async def fake_create_and_place_order(**kwargs):
        placements.append(kwargs)

    trader.client = SimpleNamespace(create_and_place_order=fake_create_and_place_order)

    async def fake_call_with_retries(fn, limiter=None):
        return await fn()

    monkeypatch.setattr("hybrid_main.call_with_retries", fake_call_with_retries)

    async def fake_check_mm_fills():
        pass

    trader._check_mm_fills = fake_check_mm_fills

    async def fake_get_signed_position():
        # Large short position -> buys allowed, sells disallowed
        return -MAX_POSITION_USD - Decimal(1)

    trader._get_signed_position = fake_get_signed_position

    await trader._market_make()

    assert cancels == ["b1"]
    assert trader._mm_buy_orders == {}
    assert placements == []

    # Second run should place a fresh buy order
    await trader._market_make()
    assert len(placements) == 1
    assert placements[0]["side"].name == "BUY"


@pytest.mark.asyncio
async def test_market_make_reprices_stale_sell(monkeypatch):
    trader = _mk_trader()

    now = time.time()
    trader._mm_sell_orders = {0: ("s1", Decimal("102"), now - 20)}
    trader._mm_sell_id = "s1"
    trader._mm_buy_orders = {}

    cancels = []

    async def fake_cancel(external_id):
        cancels.append(external_id)

    trader._safe_cancel = fake_cancel

    placements = []

    async def fake_create_and_place_order(**kwargs):
        placements.append(kwargs)

    trader.client = SimpleNamespace(create_and_place_order=fake_create_and_place_order)

    async def fake_call_with_retries(fn, limiter=None):
        return await fn()

    monkeypatch.setattr("hybrid_main.call_with_retries", fake_call_with_retries)

    async def fake_check_mm_fills():
        pass

    trader._check_mm_fills = fake_check_mm_fills

    async def fake_get_signed_position():
        # Large long position -> sells allowed, buys disallowed
        return MAX_POSITION_USD + Decimal(1)

    trader._get_signed_position = fake_get_signed_position

    await trader._market_make()

    assert cancels == ["s1"]
    assert trader._mm_sell_orders == {}
    assert placements == []

    await trader._market_make()
    assert len(placements) == 1
    assert placements[0]["side"].name == "SELL"

