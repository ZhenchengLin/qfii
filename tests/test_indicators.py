import itertools
import math

import pytest

from qfii.tools.indicators import annualized_vol, compute_all, max_drawdown, pct_return, rsi, sma


def test_pct_return():
    assert pct_return([100, 105, 110], 2) == pytest.approx(0.10)
    assert pct_return([100, 110], 2) is None


def test_sma():
    assert sma([1, 2, 3, 4, 5], 3) == 4
    assert sma([1, 2], 3) is None


def test_annualized_vol_constant_growth_is_zero():
    closes = [100 * 1.01 ** i for i in range(30)]
    assert annualized_vol(closes, 20) == pytest.approx(0, abs=1e-12)


def test_annualized_vol_alternating():
    closes = [100, 110, 100, 110, 100]
    r = math.log(1.1)
    rets = [r, -r, r, -r]
    mean = 0
    sd = math.sqrt(sum((x - mean) ** 2 for x in rets) / 3)
    assert annualized_vol(closes, 4) == pytest.approx(sd * math.sqrt(252))


def test_rsi_extremes_and_known_value():
    assert rsi(list(range(1, 30)), 14) == 100.0
    # 14 alternating +1/-1 moves then one +1: Wilder averages
    closes = [10, 11] * 7 + [10, 11]
    deltas = [b - a for a, b in itertools.pairwise(closes)]
    g = sum(max(d, 0) for d in deltas[:14]) / 14
    loss = sum(max(-d, 0) for d in deltas[:14]) / 14
    g = (g * 13 + 1) / 14
    loss = (loss * 13 + 0) / 14
    assert rsi(closes, 14) == pytest.approx(100 - 100 / (1 + g / loss))


def test_max_drawdown():
    assert max_drawdown([100, 120, 90, 130, 117], 5) == pytest.approx(90 / 120 - 1)


def test_compute_all_short_history_gives_none():
    out = compute_all([100.0] * 30)
    assert out["sma_200"]["value"] is None and out["sma_200"]["window"] == 200
    assert out["return_1m"]["value"] == 0
