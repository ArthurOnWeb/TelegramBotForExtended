# backoff_utils.py
import asyncio, random, re

async def call_with_retries(op, *, limiter, max_attempts=6, base_delay=0.25):
    # op: async function with no args (lambda) that does the REST call
    attempt = 0
    while True:
        attempt += 1
        await limiter.acquire()
        try:
            return await op()
        except Exception as e:
            msg = str(e)
            is_429 = " 429 " in msg or "Too Many Requests" in msg
            is_5xx = " 5" in msg[:5] or " 50" in msg or " 502 " in msg or " 503 " in msg or " 504 " in msg
            if not (is_429 or is_5xx):
                raise
            if attempt >= max_attempts:
                raise
            # Retry-After seconds if present
            m = re.search(r"Retry-After\"?:\s*\"?(\d+)", msg, re.IGNORECASE)
            ra = float(m.group(1)) if m else None
            # Exponential backoff + jitter
            delay = ra if ra is not None else (base_delay * (2 ** (attempt - 1)))
            delay *= (0.8 + 0.4 * random.random())
            await asyncio.sleep(min(delay, 5.0))
