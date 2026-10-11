"""The week dataset: raw downloads turned into exactly what the tools read."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from qfii.clock import ET
from qfii.tools.sec import headline
from qfii.week.dataset import (
    _company,
    daily_rows,
    draw,
    files_10k_10q,
    intraday_rows,
    select_filings,
)


def ts(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=ET).timestamp())


def chart(timestamps, closes, opens=None, volumes=None, adj=None):
    n = len(timestamps)
    quote = {"open": opens or closes, "high": closes, "low": closes, "close": closes,
             "volume": volumes or [100] * n}
    ind = {"quote": [quote]}
    if adj is not None:
        ind["adjclose"] = [{"adjclose": adj}]
    return {"chart": {"result": [{"meta": {}, "timestamp": timestamps, "indicators": ind}]}}


def test_minute_bars_become_ten_minute_bars_stamped_at_their_close():
    stamps = [ts(2026, 10, 7, 9, 30), ts(2026, 10, 7, 9, 39), ts(2026, 10, 7, 9, 40),
              ts(2026, 10, 7, 15, 59), ts(2026, 10, 7, 16, 0), ts(2026, 10, 7, 9, 29)]
    rows = intraday_rows(chart(stamps, [10, 11, 12, 20, 21, 9]), "X")
    assert [(r["time"][11:16], r["close"], r["volume"]) for r in rows] == [
        ("09:40", 11, 200), ("09:50", 12, 100), ("16:00", 21, 200)]   # pre-open bar dropped
    assert rows[0]["open"] == 10 and rows[0]["high"] == 11


def test_daily_history_rescaled_to_the_raw_price_before_the_week():
    stamps = [int(datetime(2026, 10, d, 20, tzinfo=UTC).timestamp()) for d in (1, 2, 5, 6)]
    rows = daily_rows(chart(stamps, [100, 102, 104, 103], adj=[99, 101, 103, 103]), "X",
                      date(2026, 10, 5))
    closes = {r["date"]: r["close"] for r in rows}
    assert closes["2026-10-02"] == 102                  # joins the raw level exactly
    assert closes["2026-10-01"] == pytest.approx(99 * 102 / 101, abs=1e-4)
    assert closes["2026-10-05"] == 104                  # inside the week: raw close


def test_one_symbol_per_company():
    assert _company("Alphabet Inc. Class A Common Stock") == \
        _company("Alphabet Inc. Class C Capital Stock")
    assert _company("Cisco Systems, Inc. Common Stock (DE)") != _company("Exelon Corporation")


class Filing:
    def __init__(self, form, filed, acc="a"):
        self.form, self.filing_date, self.accession_no = form, filed, acc


class Company:
    def __init__(self, filings):
        self.filings = filings

    def get_filings(self, form):
        return [f for f in self.filings if f.form.split("/")[0] == form]


def test_filing_selection_covers_what_the_week_can_see():
    co = Company([Filing("10-K", date(2026, 2, 1), "k2"), Filing("10-K", date(2025, 2, 1), "k1"),
                  Filing("10-Q", date(2026, 10, 7), "q3"), Filing("10-Q", date(2026, 7, 30), "q2"),
                  Filing("10-Q", date(2026, 4, 30), "q1"),
                  Filing("8-K", date(2026, 10, 12), "e4"), Filing("8-K", date(2026, 10, 6), "e3"),
                  Filing("8-K/A", date(2026, 9, 1), "e2a"), Filing("8-K", date(2026, 3, 1), "e1")])
    got = [f.accession_no for f in select_filings(co, date(2026, 10, 5), date(2026, 10, 9), 120)]
    assert got == ["k2", "q3", "q2", "e3"]   # q3 filed mid-week, q2 for the days before it


def test_foreign_filers_are_not_eligible():
    assert files_10k_10q(Company([Filing("20-F", date(2026, 3, 1))]), date(2026, 10, 5))
    assert files_10k_10q(Company([Filing("10-K", date(2026, 2, 1)),
                                  Filing("10-Q", date(2026, 8, 1))]), date(2026, 10, 5)) is None


def test_the_draw_is_random_but_reproducible(cfg, tmp_path):
    raw = tmp_path / "raw"
    (raw / "yahoo_1d").mkdir(parents=True), (raw / "yahoo_1m").mkdir()
    symbols = [f"S{i:02d}" for i in range(20)] + ["FOR"]
    (raw / "nasdaq100.json").write_text(json.dumps({"data": {"rows": [
        {"symbol": s, "companyName": f"{s} Corp Common Stock"} for s in symbols]}}))
    daily = [int(datetime(2025, 1, 1, 20, tzinfo=UTC).timestamp()) + 86400 * i
             for i in range(640)]
    minutes = [ts(2026, 10, d, 9, 30) + 60 * m for d in range(5, 10) for m in range(390)]
    for s in symbols:
        (raw / "yahoo_1d" / f"{s}.json").write_text(json.dumps(chart(daily, [10.0] * 640)))
        (raw / "yahoo_1m" / f"{s}.json").write_text(json.dumps(chart(minutes, [10.0] * 1950)))
    good = Company([Filing("10-K", date(2026, 2, 1)), Filing("10-Q", date(2026, 8, 1))])
    week = replace(cfg.week, raw_dir=raw, symbols=5, seed=7)
    c = replace(cfg, week=week)

    def factory(sym):
        return Company([Filing("20-F", date(2026, 3, 1))]) if sym == "FOR" else good

    first = draw(c, factory, log=lambda *_: None)
    again = draw(c, factory, log=lambda *_: None)
    other = draw(replace(c, week=replace(week, seed=8)), factory, log=lambda *_: None)
    assert first.picked == again.picked and len(first.picked) == 5
    assert first.picked != other.picked
    assert "FOR" not in first.picked


def test_headlines_skip_logos_and_cover_pages():
    assert headline("cisco\\_logo.jpg\n\n<!-- image -->\n| a | b |\n"
                    "Cisco Reports Fourth Quarter and Fiscal Year 2026 Earnings\n") == \
        "Cisco Reports Fourth Quarter and Fiscal Year 2026 Earnings"
    assert headline("UNITED STATES\nSECURITIES AND EXCHANGE COMMISSION\nFORM 8-K\n"
                    "Pursuant to Section 13 or 15(d) of the Securities Exchange Act of 1934\n"
                    "Item 5.02 Departure of Directors or Certain Officers\n") == ""
