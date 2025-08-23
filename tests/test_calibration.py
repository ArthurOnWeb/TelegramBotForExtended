import sys
from pathlib import Path

import pytest

# Ensure src/ is importable
sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from regime.calibration import VolatilityCalibrator


def test_average_and_threshold():
    calib = VolatilityCalibrator(window=5)
    values = [0.1, 0.2, 0.3, 0.4, 0.5]
    for v in values:
        calib.update("BTC", v)
    # Average of values
    assert calib.average("BTC") == pytest.approx(sum(values) / len(values))
    # 20th percentile should correspond to the smallest value in this simple set
    assert calib.threshold("BTC") == pytest.approx(0.1)


def test_empty_returns_zero():
    calib = VolatilityCalibrator()
    assert calib.average("ETH") == 0.0
    assert calib.threshold("ETH") == 0.0
