import sys
from pathlib import Path
import pytest

# ensure src directory is importable
sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from utils import _close_order_book


@pytest.mark.asyncio
async def test_stop_orderbook_preferred():
    class Dummy:
        def __init__(self):
            self.called = None
        async def stop_orderbook(self):
            self.called = "stop"
    ob = Dummy()
    await _close_order_book(ob)
    assert ob.called == "stop"


@pytest.mark.asyncio
async def test_aclose_fallback():
    class Dummy:
        def __init__(self):
            self.called = None
        async def aclose(self):
            self.called = "aclose"
    ob = Dummy()
    await _close_order_book(ob)
    assert ob.called == "aclose"


@pytest.mark.asyncio
async def test_close_last_resort_handles_sync_and_async():
    class AsyncDummy:
        def __init__(self):
            self.called = None
        async def close(self):
            self.called = "async"
    class SyncDummy:
        def __init__(self):
            self.called = False
        def close(self):
            self.called = True
    # Async close
    ob_async = AsyncDummy()
    await _close_order_book(ob_async)
    assert ob_async.called == "async"
    # Sync close
    ob_sync = SyncDummy()
    await _close_order_book(ob_sync)
    assert ob_sync.called is True
