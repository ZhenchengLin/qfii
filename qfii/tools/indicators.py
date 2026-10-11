"""Deterministic technical indicators. LLMs never compute these; they cite them."""

from __future__ import annotations

import itertools
import math
import statistics

TRADING_DAYS = 252


def pct_return(closes: list[float], window: int) -> float | None:
    if len(closes) <= window:
        return None
    return closes[-1] / closes[-1 - window] - 1


def sma(closes: list[float], window: int) -> float | None:
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def annualized_vol(closes: list[float], window: int) -> float | None:
    if len(closes) <= window:
        return None
    tail = closes[-window - 1:]
    rets = [math.log(b / a) for a, b in itertools.pairwise(tail)]
    return statistics.stdev(rets) * math.sqrt(TRADING_DAYS)


def rsi(closes: list[float], window: int = 14) -> float | None:
    """Wilder's RSI."""
    if len(closes) <= window:
        return None
    deltas = [b - a for a, b in itertools.pairwise(closes)]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:window]) / window
    avg_loss = sum(losses[:window]) / window
    for g, loss in zip(gains[window:], losses[window:]):
        avg_gain = (avg_gain * (window - 1) + g) / window
        avg_loss = (avg_loss * (window - 1) + loss) / window
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def max_drawdown(closes: list[float], window: int) -> float | None:
    if len(closes) < window:
        return None
    peak = -math.inf
    worst = 0.0
    for c in closes[-window:]:
        peak = max(peak, c)
        worst = min(worst, c / peak - 1)
    return worst


# name -> (function, window). Windows are in trading days.
INDICATORS = {
    "return_1m": (pct_return, 21),
    "return_3m": (pct_return, 63),
    "return_12m": (pct_return, 252),
    "sma_50": (sma, 50),
    "sma_200": (sma, 200),
    "volatility_3m_ann": (annualized_vol, 63),
    "rsi_14": (rsi, 14),
    "max_drawdown_12m": (max_drawdown, 252),
}


def compute_all(closes: list[float]) -> dict[str, dict]:
    out = {}
    for name, (fn, window) in INDICATORS.items():
        value = fn(closes, window)
        out[name] = {"value": None if value is None else round(value, 6), "window": window}
    return out
