"""Macro series from local FRED files, with publication lag so no figure is used
before it was public (monthly CPI for September is released mid-October)."""

from __future__ import annotations

import csv
import statistics
from datetime import date, timedelta
from pathlib import Path

from .market import DataError

SERIES_LABELS = {
    "DGS10": "10-year Treasury yield (%)",
    "DGS2": "2-year Treasury yield (%)",
    "T10Y2Y": "10y minus 2y Treasury spread (pct points)",
    "DFF": "Effective federal funds rate (%)",
    "CPIAUCSL": "CPI, all urban consumers (index)",
    "UNRATE": "Unemployment rate (%)",
    "BAMLH0A0HYM2": "High-yield credit spread, OAS (%)",
    "VIXCLS": "VIX volatility index",
}


def load_series(macro_dir: Path, series_id: str) -> list[tuple[date, float]]:
    path = macro_dir / f"{series_id}.csv"
    if not path.exists():
        raise DataError(f"no macro file for {series_id}: {path} (run `qfii data fetch --macro`)")
    with path.open(newline="") as fh:
        return [(date.fromisoformat(r["date"]), float(r["value"])) for r in csv.DictReader(fh)]


def publication_lag_days(rows: list[tuple[date, float]]) -> int:
    """Daily/weekly data: next day. Monthly: ~45 days. Quarterly: ~90 days."""
    if len(rows) < 2:
        return 45
    spacing = statistics.median((b[0] - a[0]).days for a, b in zip(rows[-13:], rows[-12:]))
    return 1 if spacing <= 7 else 45 if spacing <= 40 else 90


def _latest(rows: list[tuple[date, float]], cutoff: date) -> tuple[date, float] | None:
    eligible = [r for r in rows if r[0] <= cutoff]
    return eligible[-1] if eligible else None


def series_snapshot(rows: list[tuple[date, float]], as_of: date) -> dict:
    lag = publication_lag_days(rows)
    now = _latest(rows, as_of - timedelta(days=lag))
    if now is None:
        return {"available": False, "reason": f"no observation public by {as_of}"}
    ago3 = _latest(rows, now[0] - timedelta(days=91))
    ago12 = _latest(rows, now[0] - timedelta(days=365))
    return {
        "available": True, "value": now[1], "observation_date": now[0].isoformat(),
        "publication_lag_days": lag,
        "value_3m_earlier": ago3[1] if ago3 else None,
        "value_12m_earlier": ago12[1] if ago12 else None,
    }


def macro_snapshot(macro_dir: Path, series_ids: list[str], as_of: date) -> dict[str, dict]:
    out = {}
    for sid in series_ids:
        try:
            snap = series_snapshot(load_series(macro_dir, sid), as_of)
        except DataError as exc:
            snap = {"available": False, "reason": str(exc)}
        snap["label"] = SERIES_LABELS.get(sid, sid)
        out[sid] = snap
    return out
