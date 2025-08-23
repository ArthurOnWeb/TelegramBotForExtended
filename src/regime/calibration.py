"""Volatility calibration utilities.

This module maintains per-symbol volatility estimates over a rolling window
and derives dynamic thresholds for regime detection.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque, Dict, Iterable, List


class VolatilityCalibrator:
    """Track recent volatility measurements per symbol.

    Parameters
    ----------
    window: int
        Maximum number of volatility observations to retain for each symbol.
    """

    def __init__(self, window: int = 100) -> None:
        self._history: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=window))

    # ------------------------------------------------------------------
    def update(self, symbol: str, sigma: float) -> None:
        """Record a new volatility observation for ``symbol``."""

        self._history[symbol].append(float(sigma))

    # ------------------------------------------------------------------
    def average(self, symbol: str) -> float:
        """Return the average volatility for ``symbol``."""

        hist = self._history.get(symbol)
        if not hist:
            return 0.0
        return sum(hist) / len(hist)

    # ------------------------------------------------------------------
    def threshold(self, symbol: str, quantile: float = 0.2) -> float:
        """Return the ``quantile`` of recorded volatility for ``symbol``.

        If no observations are available, ``0.0`` is returned.
        """

        hist = self._history.get(symbol)
        if not hist:
            return 0.0
        return _quantile(hist, quantile)


# ----------------------------------------------------------------------------
# Helper


def _quantile(values: Iterable[float], q: float) -> float:
    """Simple quantile estimator for a collection of floats."""

    data: List[float] = sorted(float(v) for v in values)
    if not data:
        return 0.0
    q = min(max(q, 0.0), 1.0)
    idx = int(q * (len(data) - 1))
    return data[idx]
