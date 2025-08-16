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
