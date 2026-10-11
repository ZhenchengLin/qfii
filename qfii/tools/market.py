"""Price history sources. Every loader truncates at `as_of`, so no tool can leak
data from after the information cutoff (no look-ahead by construction)."""

from __future__ import annotations

import csv
import hashlib
import math
import random
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from ..clock import AsOf


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


@dataclass(frozen=True)
class IntradayBar:
    time: datetime          # when the bar closed, US Eastern
    close: float
    volume: float


def intraday_prices(intraday_dir: Path | None, symbol: str) -> list[IntradayBar]:
    path = intraday_dir / f"{symbol}.csv" if intraday_dir else None
    if path is None or not path.exists():
        return []
    out = []
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                out.append(IntradayBar(datetime.fromisoformat(row["time"]), float(row["close"]),
                                       float(row.get("volume") or 0)))
            except (KeyError, ValueError) as exc:
                raise DataError(f"{path}: bad row {row}") from exc
    return sorted(out, key=lambda b: b.time)


def load_prices(source: str, csv_dir: Path, symbol: str, as_of: date | str | AsOf,
                intraday_dir: Path | None = None) -> list[Bar]:
    """Daily bars public at the cutoff. With a time-of-day cutoff, today's bar is
    built from the 10-minute bars that had closed by then (last close, volume so
    far); without intraday data, today's daily bar appears only after the close."""
    cut = AsOf.parse(as_of)
    if source == "synthetic":
        bars = synthetic_prices(symbol, cut.day)
    elif source == "csv":
        bars = csv_prices(csv_dir, symbol)
    else:
        raise DataError(f"unknown data source {source!r}")
    today = cut.day.isoformat()
    if cut.at is None:
        return [b for b in bars if b.date <= today]
    out = [b for b in bars if b.date < today]
    so_far = [b for b in intraday_prices(intraday_dir, symbol)
              if b.time.date() == cut.day and cut.covers(b.time)]
    if so_far:
        out.append(Bar(today, so_far[-1].close, float(sum(b.volume for b in so_far))))
    elif not cut.intraday:
        out += [b for b in bars if b.date == today]
    return out
