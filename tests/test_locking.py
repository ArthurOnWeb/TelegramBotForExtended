import asyncio
import types
from decimal import Decimal
import sys
from pathlib import Path
import types
import pytest
import os

# Stub x10 dependencies to allow importing maker_main without the real package
dummy_x10 = types.ModuleType("x10")
perp = types.ModuleType("perpetual")
orderbook = types.ModuleType("orderbook")
orders = types.ModuleType("orders")
simple_client = types.ModuleType("simple_client")
stc = types.ModuleType("simple_trading_client")
trading_client = types.ModuleType("trading_client")

orderbook.OrderBook = object
orders.OrderSide = types.SimpleNamespace(BUY=1, SELL=2)
stc.BlockingTradingClient = object
trading_client.PerpetualTradingClient = object

simple_client.simple_trading_client = stc
perp.orderbook = orderbook
perp.orders = orders
perp.simple_client = simple_client
perp.trading_client = trading_client
dummy_x10.perpetual = perp

sys.modules.setdefault("x10", dummy_x10)
sys.modules.setdefault("x10.perpetual", perp)
sys.modules.setdefault("x10.perpetual.orderbook", orderbook)
sys.modules.setdefault("x10.perpetual.orders", orders)
sys.modules.setdefault("x10.perpetual.simple_client", simple_client)
sys.modules.setdefault("x10.perpetual.simple_client.simple_trading_client", stc)
sys.modules.setdefault("x10.perpetual.trading_client", trading_client)

# Stub account module to avoid dotenv/x10 dependencies during import
account_stub = types.ModuleType("account")
account_stub.TradingAccount = object
sys.modules.setdefault("account", account_stub)

# Provide market name to avoid interactive prompt
os.environ.setdefault("MM_MARKET", "BTC")

# Ensure src/ is on the import path
sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))
import maker_main
from maker_main import MarketMaker, Slot

# Stub out network retry helper to run the operation directly
async def _immediate(fn, *, limiter=None):
    return await fn()
maker_main.call_with_retries = _immediate

class DummyAccount:
    def __init__(self, get_open_orders):
        self.endpoint_config = None
        self._async_client = types.SimpleNamespace(
            account=types.SimpleNamespace(get_open_orders=get_open_orders)
        )

    def get_async_client(self):
        return self._async_client

    def get_blocking_client(self):
        return None

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_detect_reconcile_allows_parallel_placement():
    start_evt = asyncio.Event()
    finish_evt = asyncio.Event()

    async def fake_get_open_orders(market_names):
        start_evt.set()
        await finish_evt.wait()
        return types.SimpleNamespace(data=[])

    mm = MarketMaker(DummyAccount(fake_get_open_orders), "BTC")
    mm._market = types.SimpleNamespace(name="BTC")

    # Start detect_reconcile which will block until finish_evt is set
    task = asyncio.create_task(mm.detect_reconcile())
    await start_evt.wait()

    # Lock should be free while network call is in flight
    await asyncio.wait_for(mm._placement_lock.acquire(), timeout=0.1)
    mm._placement_lock.release()

    finish_evt.set()
    await task


@pytest.mark.asyncio
async def test_reconcile_updates_slots_under_lock():
    mm = MarketMaker(DummyAccount(lambda m: types.SimpleNamespace(data=[])), "BTC")
    mm._market = types.SimpleNamespace(name="BTC")
    mm._buy_slots = [Slot("ext", Decimal("1"))]

    events: list[str] = []

    class TrackingLock(asyncio.Lock):
        async def __aenter__(self):
            events.append("enter")
            await super().__aenter__()
            return self

        async def __aexit__(self, exc_type, exc, tb):
            events.append("exit")
            await super().__aexit__(exc_type, exc, tb)

    mm._placement_lock = TrackingLock()
    await mm.reconcile([(mm._buy_slots, 0)], [])

    assert mm._buy_slots[0].external_id is None
    assert events == ["enter", "exit"]


@pytest.mark.asyncio
async def test_detect_reconcile_marks_price_drift_order():
    drift_order = types.SimpleNamespace(
        external_id="mm_1", price=Decimal("2"), id=1
    )

    async def fake_get_open_orders(market_names):
        return types.SimpleNamespace(data=[drift_order])

    mm = MarketMaker(DummyAccount(fake_get_open_orders), "BTC")
    mm._market = types.SimpleNamespace(name="BTC")
    mm._buy_slots = [Slot("mm_1", Decimal("1"))]

    missing, orphans, open_orders = await mm.detect_reconcile()

    assert missing == [(mm._buy_slots, 0)]
    assert orphans == [drift_order]
    assert open_orders == [drift_order]
