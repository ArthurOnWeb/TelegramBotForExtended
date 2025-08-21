import asyncio
import time
import sys
from pathlib import Path

import pytest

# Ensure src/ is importable
sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from rate_limit import TokenBucket


@pytest.mark.asyncio
async def test_tokens_refill_over_time():
    bucket = TokenBucket(capacity=5, refill_per_sec=10)
    # Drain the bucket completely
    for _ in range(5):
        await bucket.acquire()

    # Allow some time for tokens to refill
    await asyncio.sleep(0.3)

    # Trigger token recalculation without consuming
    await bucket.acquire(0)

    assert bucket.tokens == pytest.approx(3, abs=0.3)


@pytest.mark.asyncio
async def test_acquire_waits_for_refill():
    bucket = TokenBucket(capacity=1, refill_per_sec=5)
    await bucket.acquire()

    start = time.monotonic()
    await bucket.acquire()
    elapsed = time.monotonic() - start

    assert elapsed >= 0.18
    assert elapsed < 0.5
