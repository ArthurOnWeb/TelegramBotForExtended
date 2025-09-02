"""Helper to retry REST calls with exponential backoff."""

import asyncio
import contextlib
import os
import random
import re


# Maximum duration allowed for each REST call.
REQUEST_TIMEOUT = float(os.getenv("MM_REQUEST_TIMEOUT", "10"))


async def call_with_retries(op, *, limiter, max_attempts=6, base_delay=0.25):
    """Execute ``op`` with retry/backoff logic.

    ``op`` is an async function (no-arg lambda) performing the REST call.
    ``limiter`` controls the request rate.
    ``max_attempts`` and ``base_delay`` configure exponential backoff.
    """

    attempt = 0
    while True:
        attempt += 1
        await limiter.acquire()
        try:
            task = asyncio.create_task(op())
            try:
                return await asyncio.wait_for(task, timeout=REQUEST_TIMEOUT)
            except asyncio.TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                raise
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: PERF203 - we want to catch broadly
            status_code = getattr(e, "status_code", None)
            msg = str(e)
            is_timeout = isinstance(e, asyncio.TimeoutError)
            if status_code is not None:
                is_429 = status_code == 429
                is_5xx = 500 <= status_code <= 599
            else:
                is_429 = " 429 " in msg or "Too Many Requests" in msg
                is_5xx = (
                    " 5" in msg[:5]
                    or " 50" in msg
                    or " 502 " in msg
                    or " 503 " in msg
                    or " 504 " in msg
                )
            if not (is_timeout or is_429 or is_5xx):
                raise
            if attempt >= max_attempts:
                raise
            # Retry-After seconds if present (HTTP errors only)
            m = None if is_timeout else re.search(r"Retry-After\"?:\s*\"?(\d+)", msg, re.IGNORECASE)
            ra = float(m.group(1)) if m else None
            # Exponential backoff + jitter
            delay = ra if ra is not None else (base_delay * (2 ** (attempt - 1)))
            delay *= 0.8 + 0.4 * random.random()
            await asyncio.sleep(min(delay, 5.0))
