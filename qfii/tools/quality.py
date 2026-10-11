"""Deterministic data-quality gate. Runs before any LLM sees the data."""

from __future__ import annotations

from datetime import date

from .market import Bar


def quality_flags(bars: list[Bar], as_of: date, *, source: str, min_observations: int,
                  stale_after_days: int) -> list[str]:
    flags = []
    if not bars:
        return ["short_sample", "missing_values"]
    last = date.fromisoformat(bars[-1].date)
    if (as_of - last).days > stale_after_days:
        flags.append("stale_data")
    if len(bars) < min_observations:
        flags.append("short_sample")
    if any(b.close <= 0 for b in bars) or len({b.date for b in bars}) != len(bars):
        flags.append("missing_values")
    if source == "synthetic":
        flags.append("synthetic_data")
    return flags
