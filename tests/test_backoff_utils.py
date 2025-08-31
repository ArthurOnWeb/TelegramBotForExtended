import os
import asyncio
import importlib
import sys
from pathlib import Path

import pytest

# Ensure src/ is importable
sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))


def _reload_module():
    import backoff_utils
    importlib.reload(backoff_utils)
    return backoff_utils


@pytest.mark.asyncio
async def test_retry_on_timeout():
    os.environ["MM_REQUEST_TIMEOUT"] = "0.05"
    backoff_utils = _reload_module()
    attempts = 0

    async def op():
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0.1)

    class DummyLimiter:
        async def acquire(self):
            pass

    limiter = DummyLimiter()
    with pytest.raises(asyncio.TimeoutError):
        await backoff_utils.call_with_retries(op, limiter=limiter, max_attempts=3, base_delay=0.01)

    assert attempts == 3


class StatusError(Exception):
    def __init__(self, code):
        super().__init__("boom")
        self.status_code = code


@pytest.mark.asyncio
async def test_retry_on_429_status_code():
    backoff_utils = _reload_module()
    attempts = 0

    async def op():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise StatusError(429)
        return "ok"

    class DummyLimiter:
        async def acquire(self):
            pass

    limiter = DummyLimiter()
    result = await backoff_utils.call_with_retries(op, limiter=limiter, max_attempts=2, base_delay=0.01)
    assert result == "ok"
    assert attempts == 2


@pytest.mark.asyncio
async def test_retry_on_5xx_status_code():
    backoff_utils = _reload_module()
    attempts = 0

    async def op():
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise StatusError(503)
        return "ok"

    class DummyLimiter:
        async def acquire(self):
            pass

    limiter = DummyLimiter()
    result = await backoff_utils.call_with_retries(op, limiter=limiter, max_attempts=3, base_delay=0.01)
    assert result == "ok"
    assert attempts == 2


@pytest.mark.asyncio
async def test_cancelled_stops_retries():
    backoff_utils = _reload_module()
    attempts = 0
    started = asyncio.Event()

    async def op():
        nonlocal attempts
        attempts += 1
        started.set()
        await asyncio.sleep(1)

    class DummyLimiter:
        async def acquire(self):
            pass

    limiter = DummyLimiter()
    task = asyncio.create_task(
        backoff_utils.call_with_retries(op, limiter=limiter, max_attempts=5, base_delay=0.01)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempts == 1
