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


LABELS = {
    "return_1m": "1-month price return (21 trading days)",
    "return_3m": "3-month price return (63 trading days)",
    "return_12m": "12-month price return (252 trading days)",
    "sma_50": "50-day simple moving average of close",
    "sma_200": "200-day simple moving average of close",
    "volatility_3m_ann": "annualized volatility of daily log returns, last 63 trading days",
    "rsi_14": "14-day RSI (Wilder)",
    "max_drawdown_12m": "maximum peak-to-trough drawdown, last 252 trading days",
    "excess_return_3m": "3-month return minus benchmark 3-month return",
    "excess_return_12m": "12-month return minus benchmark 12-month return",
    "beta_12m": "beta of daily log returns vs benchmark, last 252 trading days",
    "correlation_12m": "correlation of daily log returns with benchmark, last 252 trading days",
    "avg_daily_dollar_volume_20d": "average daily dollar volume (close x shares), last 20 trading days",
}


def compute_all(closes: list[float]) -> dict[str, dict]:
    out = {}
    for name, (fn, window) in INDICATORS.items():
        value = fn(closes, window)
        out[name] = {"value": None if value is None else round(value, 6), "window": window}
    return out


def benchmark_stats(symbol_bars: list[tuple[str, float]], bench_bars: list[tuple[str, float]],
                    window: int = TRADING_DAYS) -> dict[str, float | None]:
    """Excess returns vs the benchmark and 12-month beta / correlation, on dates both have."""
    bench = dict(bench_bars)
    pairs = [(c, bench[d]) for d, c in symbol_bars if d in bench]
    out: dict[str, float | None] = {}
    for name, days in (("excess_return_3m", 63), ("excess_return_12m", 252)):
        if len(pairs) > days:
            s = pairs[-1][0] / pairs[-1 - days][0] - 1
            b = pairs[-1][1] / pairs[-1 - days][1] - 1
            out[name] = round(s - b, 6)
        else:
            out[name] = None
    tail = pairs[-window - 1:]
    if len(tail) > 20:
        rs = [math.log(b[0] / a[0]) for a, b in itertools.pairwise(tail)]
        rb = [math.log(b[1] / a[1]) for a, b in itertools.pairwise(tail)]
        var_b = statistics.variance(rb)
        out["beta_12m"] = round(statistics.covariance(rs, rb) / var_b, 6) if var_b else None
        try:
            out["correlation_12m"] = round(statistics.correlation(rs, rb), 6)
        except statistics.StatisticsError:  # one series is constant
            out["correlation_12m"] = None
    else:
        out["beta_12m"] = out["correlation_12m"] = None
    return out
