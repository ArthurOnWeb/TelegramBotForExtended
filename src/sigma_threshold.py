"""Stream volatility and dynamic sigma thresholds.

This small utility connects to the X10 order book for the chosen market and
continuously prints the realised volatility (``sigma``) together with the
current threshold derived by :class:`regime.calibration.VolatilityCalibrator`.

The script mirrors the logic used by the hybrid strategy to estimate
volatility.  Running it for a while gives an idea of the volatility distribution
and helps choosing an appropriate ``HYB_SIGMA_THRESHOLD`` value.

Example
-------
Run the script directly::

    python -m sigma_threshold

Environment variables ``HYB_MARKET``, ``HYB_REFRESH_INTERVAL`` and
``HYB_VOL_WINDOW_SEC`` can be used to tweak the behaviour.
"""

from __future__ import annotations

import asyncio
import os
from collections import deque
from typing import Deque

from x10.perpetual.configuration import MAINNET_CONFIG
from x10.perpetual.orderbook import OrderBook

from regime.calibration import VolatilityCalibrator
from utils import logger, setup_logging

# ----------------------------------------------------------------------------
# Configuration

REFRESH_INTERVAL_SEC = float(os.getenv("HYB_REFRESH_INTERVAL", "1"))
VOL_WINDOW_SEC = int(os.getenv("HYB_VOL_WINDOW_SEC", "300"))
DEFAULT_SIGMA_THRESHOLD = float(os.getenv("HYB_SIGMA_THRESHOLD", "0.0012"))
PRICE_HISTORY_LEN = max(1, int(VOL_WINDOW_SEC / REFRESH_INTERVAL_SEC))


# ----------------------------------------------------------------------------
# Helpers


def realised_vol(history: Deque[float]) -> float:
    """Return simple realised volatility of mid prices in ``history``."""
    if len(history) < 2:
        return 0.0
    rets = [history[i] / history[i - 1] - 1 for i in range(1, len(history))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return var ** 0.5


# ----------------------------------------------------------------------------
# Main loop


async def _run() -> None:
    market_name = os.getenv("HYB_MARKET") or input("Market ? ")
    orderbook = await OrderBook.create(
        MAINNET_CONFIG, market_name=market_name, start=True
    )
    mid_history: Deque[float] = deque(maxlen=PRICE_HISTORY_LEN)
    calibrator = VolatilityCalibrator()

    try:
        while True:
            bid = getattr(orderbook, "best_bid", None)
            ask = getattr(orderbook, "best_ask", None)
            if bid and ask:
                mid = float((bid.price + ask.price) / 2)
                mid_history.append(mid)
                sigma = realised_vol(mid_history)
                calibrator.update(market_name, sigma)
                threshold = calibrator.threshold(market_name)
                if threshold == 0.0:
                    threshold = DEFAULT_SIGMA_THRESHOLD
                logger.info("sigma=%.5f threshold=%.5f", sigma, threshold)
            await asyncio.sleep(REFRESH_INTERVAL_SEC)
    finally:
        await orderbook.close()


# ----------------------------------------------------------------------------
# Entrypoint


if __name__ == "__main__":
    setup_logging()
    asyncio.run(_run())
