"""Price history sources. Every loader truncates at `as_of`, so no tool can leak
data from after the information cutoff (no look-ahead by construction)."""

from __future__ import annotations

import csv
import hashlib
import math
import random
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path


@dataclass(frozen=True)
class Bar:
    date: str
    close: float
    volume: float

    def to_dict(self) -> dict:
        return asdict(self)


class DataError(RuntimeError):
    pass


def _business_days(end: date, n: int) -> list[date]:
    days: list[date] = []
    d = end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days[::-1]


def synthetic_prices(symbol: str, end: date, n: int = 400) -> list[Bar]:
    """Deterministic geometric random walk seeded by the symbol. Development only."""
    seed = int(hashlib.sha256(symbol.encode()).hexdigest()[:12], 16)
    rng = random.Random(seed)
    drift = rng.uniform(-0.10, 0.25) / 252
    vol = rng.uniform(0.15, 0.45) / math.sqrt(252)
    price = rng.uniform(20, 400)
    bars = []
    # Generate from a fixed anchor so the same symbol has the same history
    # regardless of as_of; then cut at `end`.
    anchor = date(2027, 12, 31)
    all_days = _business_days(anchor, 1500)
    for day in all_days:
        price *= math.exp(drift - vol * vol / 2 + vol * rng.gauss(0, 1))
        volume = round(rng.uniform(0.5, 1.5) * 5_000_000)
        bars.append(Bar(day.isoformat(), round(price, 4), float(volume)))
    cut = [b for b in bars if b.date <= end.isoformat()]
    return cut[-n:]


def csv_prices(csv_dir: Path, symbol: str) -> list[Bar]:
    path = csv_dir / f"{symbol}.csv"
    if not path.exists():
        raise DataError(f"no price file for {symbol}: {path}")
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    bars = []
    for row in rows:
        try:
            bars.append(Bar(row["date"], float(row["close"]), float(row.get("volume") or 0)))
        except (KeyError, ValueError) as exc:
            raise DataError(f"{path}: bad row {row}") from exc
    return sorted(bars, key=lambda b: b.date)


def load_prices(source: str, csv_dir: Path, symbol: str, as_of: date) -> list[Bar]:
    if source == "synthetic":
        bars = synthetic_prices(symbol, as_of)
    elif source == "csv":
        bars = csv_prices(csv_dir, symbol)
    else:
        raise DataError(f"unknown data source {source!r}")
    return [b for b in bars if b.date <= as_of.isoformat()]
