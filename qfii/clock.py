"""The information cutoff (`as_of`) every tool honours.

Two forms are accepted:
  "2026-10-07"         end of that day (the original, daily resolution)
  "2026-10-07T11:20"   that minute, US Eastern time (market time)

A tool may use a data point only if it was public at the cutoff: a 10-minute
price bar once the bar has closed, a filing once the SEC accepted it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


class CutoffError(ValueError):
    pass


@dataclass(frozen=True)
class AsOf:
    day: date
    at: datetime | None = None          # tz-aware, Eastern; None = end of day

    @classmethod
    def parse(cls, value: str | date | datetime | AsOf) -> AsOf:
        if isinstance(value, AsOf):
            return value
        if isinstance(value, datetime):
            at = value.astimezone(ET) if value.tzinfo else value.replace(tzinfo=ET)
            return cls(at.date(), at.replace(second=0, microsecond=0))
        if isinstance(value, date):
            return cls(value)
        text = str(value).strip()
        try:
            if "T" in text or " " in text:
                at = datetime.fromisoformat(text.replace(" ", "T"))
                return cls.parse(at)
            return cls(date.fromisoformat(text))
        except ValueError as exc:
            raise CutoffError(f"as_of must be YYYY-MM-DD or YYYY-MM-DDTHH:MM (US Eastern), "
                              f"got {value!r}") from exc

    def iso(self) -> str:
        return self.day.isoformat() if self.at is None else self.at.strftime("%Y-%m-%dT%H:%M")

    __str__ = iso

    @property
    def end(self) -> datetime:
        """The last moment covered, in Eastern time."""
        return self.at or datetime.combine(self.day, time(23, 59, 59), ET)

    def covers(self, moment: datetime) -> bool:
        """Was something stamped at `moment` (naive = Eastern) public at this cutoff?"""
        m = moment if moment.tzinfo else moment.replace(tzinfo=ET)
        return m <= self.end

    @property
    def intraday(self) -> bool:
        """A cutoff during the trading day, where today's bar is still forming."""
        return self.at is not None and self.at.timetz().replace(tzinfo=None) < MARKET_CLOSE


def trading_slots(start: date, end: date, minutes: int = 10,
                  holidays: frozenset[date] = frozenset()) -> list[AsOf]:
    """Every `minutes` mark that closes a bar during regular hours, weekdays only:
    09:40, 09:50, ..., 16:00 for 10-minute slots (39 per day)."""
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5 and d not in holidays:
            t = datetime.combine(d, MARKET_OPEN, ET) + timedelta(minutes=minutes)
            close = datetime.combine(d, MARKET_CLOSE, ET)
            while t <= close:
                out.append(AsOf(d, t))
                t += timedelta(minutes=minutes)
        d += timedelta(days=1)
    return out
