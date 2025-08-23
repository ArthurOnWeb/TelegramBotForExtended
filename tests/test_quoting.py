import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure src/ is importable
sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from strategy.quoting import average_depth, fit_k_lambda, Quoter


def test_average_depth_and_refresh():
    ob = SimpleNamespace(
        bids=[(100, 2), (99.5, 3)],
        asks=[(100.5, 4), (101, 5)],
    )
    depth = average_depth(ob, levels=2)
    assert depth == pytest.approx((2 + 3 + 4 + 5) / 4)

    sigmas = [0.1, 0.2, 0.3, 0.4]
    depths = [100, 200, 300, 400]
    k_vals = [1.0, 1.5, 2.0, 2.5]
    lam_vals = [0.5, 0.7, 0.9, 1.1]

    beta_k, beta_lambda = fit_k_lambda(sigmas, depths, k_vals, lam_vals)
    quoter = Quoter(ob, beta_k, beta_lambda)
    k, lam = quoter.refresh_parameters(0.25)

    assert isinstance(k, float)
    assert isinstance(lam, float)
