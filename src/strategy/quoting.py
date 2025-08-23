"""Utilities for computing quoting parameters.

This module extracts market depth information and calibrates the
quoting parameters ``k`` and ``lambda`` (λ) as functions of
volatility ``sigma`` and available depth.  Parameters are updated on
market events by :class:`Quoter`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, Tuple

import numpy as np


def average_depth(orderbook, levels: int = 5) -> float:
    """Compute the average quantity available on the top *levels* of the book.

    The *orderbook* object is expected to expose ``bids`` and ``asks``
    attributes, each being an iterable of ``(price, size)`` pairs
    ordered from best to worst.  Missing data results in ``0.0``.
    """
    depths = []
    for side in ("bids", "asks"):
        entries = getattr(orderbook, side, [])
        for _price, size in list(entries)[:levels]:
            depths.append(float(size))
    return float(sum(depths) / len(depths)) if depths else 0.0


def fit_k_lambda(
    sigmas: Sequence[float],
    depths: Sequence[float],
    k_values: Sequence[float],
    lambda_values: Sequence[float],
) -> Tuple[np.ndarray, np.ndarray]:
    """Fit linear models for ``k`` and ``lambda``.

    A simple multiple linear regression is used so that

    ``k = a*sigma + b*depth + c``
    ``lambda = d*sigma + e*depth + f``

    The function returns two arrays ``[a, b, c]`` and ``[d, e, f]``.
    """
    X = np.column_stack([sigmas, depths, np.ones(len(sigmas))])
    beta_k, *_ = np.linalg.lstsq(X, k_values, rcond=None)
    beta_lambda, *_ = np.linalg.lstsq(X, lambda_values, rcond=None)
    return beta_k, beta_lambda


def compute_k_lambda(
    sigma: float,
    depth: float,
    beta_k: Sequence[float],
    beta_lambda: Sequence[float],
) -> Tuple[float, float]:
    """Compute ``k`` and ``lambda`` using regression coefficients."""
    k = beta_k[0] * sigma + beta_k[1] * depth + beta_k[2]
    lam = beta_lambda[0] * sigma + beta_lambda[1] * depth + beta_lambda[2]
    return float(k), float(lam)


@dataclass
class Quoter:
    """Helper to refresh quoting parameters on market updates."""

    orderbook: object
    beta_k: Sequence[float]
    beta_lambda: Sequence[float]
    levels: int = 5

    k: float = 0.0
    lambda_: float = 0.0

    def refresh_parameters(self, sigma: float) -> Tuple[float, float]:
        """Recompute ``k`` and ``lambda`` for the latest market state."""
        depth = average_depth(self.orderbook, levels=self.levels)
        self.k, self.lambda_ = compute_k_lambda(
            sigma, depth, self.beta_k, self.beta_lambda
        )
        return self.k, self.lambda_
