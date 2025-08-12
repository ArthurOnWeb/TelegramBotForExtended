# rate_limit.py
import asyncio, time, os

def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except Exception:
        return default

class TokenBucket:
    def __init__(self, capacity: int, refill_per_sec: float):
        self.capacity = capacity
        self.tokens = capacity
        self.refill_per_sec = refill_per_sec
        self._lock = asyncio.Lock()
        self._last = time.monotonic()

    async def acquire(self, n: int = 1):
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
                if self.tokens >= n:
                    self.tokens -= n
                    return
                need = (n - self.tokens) / self.refill_per_sec
            await asyncio.sleep(max(need, 0.005))

def build_rate_limiter():
    # If you’ve got MM rate-limit, set MM_MARKET_MAKER=1
    is_mm = os.getenv("MM_MARKET_MAKER", "0") == "1"
    # Default “safe” profiles (override with env if you want)
    rps = _env_int("MM_RATE_LIMIT_RPS", 200 if is_mm else 8)
    burst = _env_int("MM_RATE_LIMIT_BURST", rps * 2)  # small burst cushion
    return TokenBucket(capacity=burst, refill_per_sec=float(rps))
