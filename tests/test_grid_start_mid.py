import os
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

# Avoid interactive prompts during import of grid_main
os.environ.setdefault("GRID_MARKET", "TEST-USD")
os.environ.setdefault("GRID_LEVELS", "4")
os.environ.setdefault("GRID_MIN_PRICE", "0")
os.environ.setdefault("GRID_MAX_PRICE", "1000")

# Ensure src/ is importable
sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from grid_main import GridTrader, OrderSide  # noqa: E402


class StubAccount:
    endpoint_config = SimpleNamespace()

    def __init__(self, client):
        self._client = client

    def get_blocking_client(self):
        return self._client

    def get_async_client(self):  # pragma: no cover - not used
        return SimpleNamespace()

    async def close(self):  # pragma: no cover - simple stub
        pass


class StubOrderBook:
    def __init__(self, bid: Decimal, ask: Decimal):
        self._bid = bid
        self._ask = ask

    def best_bid(self):
        return SimpleNamespace(price=self._bid)

    def best_ask(self):
        return SimpleNamespace(price=self._ask)

    async def close(self):  # pragma: no cover - simple stub
        pass


class EmptyOrderBook:
    """Order book stub returning no bid/ask prices."""

    def best_bid(self):
        return None

    def best_ask(self):
        return None

    async def close(self):  # pragma: no cover - simple stub
        pass


@pytest.mark.asyncio
async def test_start_uses_live_mid_for_sides(monkeypatch):
    async def fake_get_markets():
        market = SimpleNamespace(
            name="TEST-USD",
            trading_config=SimpleNamespace(price_precision=1),
        )
        return {"TEST-USD": market}

    async def fake_mass_cancel(markets=None):  # pragma: no cover - simple stub
        return None

    client = SimpleNamespace(get_markets=fake_get_markets, mass_cancel=fake_mass_cancel)
    account = StubAccount(client)

    async def fake_call_with_retries(fn, limiter=None):
        return await fn()

    monkeypatch.setattr("grid_main.call_with_retries", fake_call_with_retries)

    async def fake_create_order_book(self):
        return StubOrderBook(Decimal("99"), Decimal("101"))

    monkeypatch.setattr(GridTrader, "_create_order_book", fake_create_order_book)

    async def fake_update_grid(self):
        return None

    async def fake_refresh_loop(self):
        return None

    monkeypatch.setattr(GridTrader, "_update_grid", fake_update_grid)
    monkeypatch.setattr(GridTrader, "_refresh_loop", fake_refresh_loop)

    trader = GridTrader(
        account=account,
        market_name="TEST-USD",
        grid_step=Decimal("10"),
        level_count=4,
        order_size_usd=Decimal("10"),
        lower_bound=Decimal("90"),
        upper_bound=Decimal("130"),
    )

    await trader.start()
    sides = [slot.side for slot in trader._slots]
    assert sides == [OrderSide.BUY, OrderSide.SELL, OrderSide.SELL, OrderSide.SELL]
    await trader.stop()


@pytest.mark.asyncio
async def test_start_aborts_when_mid_outside_bounds(monkeypatch):
    async def fake_get_markets():
        market = SimpleNamespace(
            name="TEST-USD",
            trading_config=SimpleNamespace(price_precision=1),
        )
        return {"TEST-USD": market}

    async def fake_mass_cancel(markets=None):  # pragma: no cover - simple stub
        return None

    client = SimpleNamespace(get_markets=fake_get_markets, mass_cancel=fake_mass_cancel)
    account = StubAccount(client)

    async def fake_call_with_retries(fn, limiter=None):
        return await fn()

    monkeypatch.setattr("grid_main.call_with_retries", fake_call_with_retries)

    async def fake_create_order_book(self):
        return StubOrderBook(Decimal("99"), Decimal("101"))

    monkeypatch.setattr(GridTrader, "_create_order_book", fake_create_order_book)

    async def fake_update_grid(self):
        return None

    async def fake_refresh_loop(self):
        return None

    monkeypatch.setattr(GridTrader, "_update_grid", fake_update_grid)
    monkeypatch.setattr(GridTrader, "_refresh_loop", fake_refresh_loop)

    trader = GridTrader(
        account=account,
        market_name="TEST-USD",
        grid_step=Decimal("10"),
        level_count=2,
        order_size_usd=Decimal("10"),
        lower_bound=Decimal("120"),
        upper_bound=Decimal("130"),
    )

    with pytest.raises(RuntimeError):
        await trader.start()


@pytest.mark.asyncio
async def test_start_uses_market_stats_when_order_book_empty(monkeypatch):
    async def fake_get_markets():
        market = SimpleNamespace(
            name="TEST-USD",
            trading_config=SimpleNamespace(price_precision=1),
            market_stats=SimpleNamespace(bid_price=Decimal("100"), ask_price=Decimal("120")),
        )
        return {"TEST-USD": market}

    async def fake_mass_cancel(markets=None):  # pragma: no cover - simple stub
        return None

    client = SimpleNamespace(
        get_markets=fake_get_markets,
        mass_cancel=fake_mass_cancel,
    )
    account = StubAccount(client)

    async def fake_call_with_retries(fn, limiter=None):
        return await fn()

    monkeypatch.setattr("grid_main.call_with_retries", fake_call_with_retries)

    async def fake_create_order_book(self):
        return EmptyOrderBook()

    monkeypatch.setattr(GridTrader, "_create_order_book", fake_create_order_book)

    async def fake_update_grid(self):
        return None

    async def fake_refresh_loop(self):
        return None

    monkeypatch.setattr(GridTrader, "_update_grid", fake_update_grid)
    monkeypatch.setattr(GridTrader, "_refresh_loop", fake_refresh_loop)

    trader = GridTrader(
        account=account,
        market_name="TEST-USD",
        grid_step=Decimal("10"),
        level_count=4,
        order_size_usd=Decimal("10"),
        lower_bound=Decimal("90"),
        upper_bound=Decimal("130"),
    )

    await trader.start()
    sides = [slot.side for slot in trader._slots]
    assert sides == [OrderSide.BUY, OrderSide.BUY, OrderSide.SELL, OrderSide.SELL]
    await trader.stop()


@pytest.mark.asyncio
async def test_start_uses_ticker_when_stats_missing(monkeypatch):
    async def fake_get_markets():
        market = SimpleNamespace(
            name="TEST-USD",
            trading_config=SimpleNamespace(price_precision=1),
            last_price=None,
            oracle_price=None,
            market_stats=None,
        )
        return {"TEST-USD": market}

    async def fake_mass_cancel(markets=None):  # pragma: no cover - simple stub
        return None

    async def fake_get_market_ticker(market_name):
        return SimpleNamespace(price=Decimal("115"))

    client = SimpleNamespace(
        get_markets=fake_get_markets,
        mass_cancel=fake_mass_cancel,
        get_market_ticker=fake_get_market_ticker,
    )
    account = StubAccount(client)

    async def fake_call_with_retries(fn, limiter=None):
        return await fn()

    monkeypatch.setattr("grid_main.call_with_retries", fake_call_with_retries)

    async def fake_create_order_book(self):
        return EmptyOrderBook()

    monkeypatch.setattr(GridTrader, "_create_order_book", fake_create_order_book)

    async def fake_update_grid(self):
        return None

    async def fake_refresh_loop(self):
        return None

    monkeypatch.setattr(GridTrader, "_update_grid", fake_update_grid)
    monkeypatch.setattr(GridTrader, "_refresh_loop", fake_refresh_loop)

    trader = GridTrader(
        account=account,
        market_name="TEST-USD",
        grid_step=Decimal("10"),
        level_count=4,
        order_size_usd=Decimal("10"),
        lower_bound=Decimal("90"),
        upper_bound=Decimal("130"),
    )

    await trader.start()
    sides = [slot.side for slot in trader._slots]
    assert sides == [OrderSide.BUY, OrderSide.BUY, OrderSide.SELL, OrderSide.SELL]
    await trader.stop()


@pytest.mark.asyncio
async def test_start_errors_when_all_price_sources_missing(monkeypatch):
    async def fake_get_markets():
        market = SimpleNamespace(
            name="TEST-USD",
            trading_config=SimpleNamespace(price_precision=1),
            last_price=None,
            oracle_price=None,
            market_stats=None,
        )
        return {"TEST-USD": market}

    async def fake_mass_cancel(markets=None):  # pragma: no cover - simple stub
        return None

    client = SimpleNamespace(get_markets=fake_get_markets, mass_cancel=fake_mass_cancel)
    account = StubAccount(client)

    async def fake_call_with_retries(fn, limiter=None):
        return await fn()

    monkeypatch.setattr("grid_main.call_with_retries", fake_call_with_retries)

    async def fake_create_order_book(self):
        return EmptyOrderBook()

    monkeypatch.setattr(GridTrader, "_create_order_book", fake_create_order_book)

    async def fake_update_grid(self):
        return None

    async def fake_refresh_loop(self):
        return None

    monkeypatch.setattr(GridTrader, "_update_grid", fake_update_grid)
    monkeypatch.setattr(GridTrader, "_refresh_loop", fake_refresh_loop)

    trader = GridTrader(
        account=account,
        market_name="TEST-USD",
        grid_step=Decimal("10"),
        level_count=4,
        order_size_usd=Decimal("10"),
        lower_bound=Decimal("90"),
        upper_bound=Decimal("130"),
    )

    with pytest.raises(RuntimeError):
        await trader.start()

