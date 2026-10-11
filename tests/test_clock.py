"""Minute-level information cutoffs: prices and filings appear only once public."""

import json
from datetime import date, datetime

import pytest

from qfii.clock import ET, AsOf, CutoffError, trading_slots
from qfii.tools.market import load_prices
from qfii.tools.sec import ManifestFiling, SecSource, parse_accepted, visible


def test_parse_and_format():
    assert AsOf.parse("2026-10-07").iso() == "2026-10-07"
    cut = AsOf.parse("2026-10-07T11:20")
    assert cut.iso() == "2026-10-07T11:20" and cut.at.tzinfo is ET and cut.intraday
    assert not AsOf.parse("2026-10-07T16:00").intraday
    assert AsOf.parse(date(2026, 10, 7)).at is None
    with pytest.raises(CutoffError):
        AsOf.parse("10/07/2026")


def test_trading_slots_cover_regular_hours_in_ten_minute_steps():
    slots = trading_slots(date(2026, 10, 5), date(2026, 10, 11))   # Mon..Sun
    assert len(slots) == 5 * 39
    assert slots[0].iso() == "2026-10-05T09:40" and slots[38].iso() == "2026-10-05T16:00"
    assert slots[-1].iso() == "2026-10-09T16:00"


@pytest.fixture
def prices(tmp_path):
    daily, intraday = tmp_path / "prices", tmp_path / "intraday"
    daily.mkdir(), intraday.mkdir()
    (daily / "ABC.csv").write_text("date,close,volume\n2026-10-05,100,1000\n"
                                   "2026-10-06,101,1000\n2026-10-07,110,3000\n")
    (intraday / "ABC.csv").write_text(
        "time,open,high,low,close,volume\n"
        "2026-10-07T09:40:00-04:00,101,103,100,102,500\n"
        "2026-10-07T09:50:00-04:00,102,106,102,105,700\n"
        "2026-10-07T10:00:00-04:00,105,108,104,107,400\n")
    return daily, intraday


def test_daily_cutoff_unchanged(prices):
    daily, intraday = prices
    bars = load_prices("csv", daily, "ABC", "2026-10-07", intraday)
    assert [b.close for b in bars] == [100, 101, 110]


def test_minute_cutoff_builds_today_from_closed_bars(prices):
    daily, intraday = prices
    bars = load_prices("csv", daily, "ABC", "2026-10-07T09:50", intraday)
    assert [b.close for b in bars] == [100, 101, 105]           # the daily 110 is the future
    assert bars[-1].volume == 1200 and bars[-1].date == "2026-10-07"
    # 09:45: only the 09:40 bar has closed
    assert load_prices("csv", daily, "ABC", "2026-10-07T09:45", intraday)[-1].close == 102
    # before the first bar closes, today does not exist yet
    assert [b.date for b in load_prices("csv", daily, "ABC", "2026-10-07T09:35", intraday)] \
        == ["2026-10-05", "2026-10-06"]


def test_minute_cutoff_without_intraday_hides_today_until_the_close(prices):
    daily, _ = prices
    assert load_prices("csv", daily, "ABC", "2026-10-07T11:00")[-1].date == "2026-10-06"
    assert load_prices("csv", daily, "ABC", "2026-10-07T16:00")[-1].close == 110


def test_sec_accepted_time_parsed_from_index_page():
    html = '<div class="infoHead">Accepted</div>\n<div class="info">2026-07-30 16:30:28</div>'
    when = parse_accepted(html)
    assert when == datetime(2026, 7, 30, 16, 30, 28, tzinfo=ET)


def test_filing_visible_only_after_acceptance():
    accepted = datetime(2026, 10, 7, 16, 5, tzinfo=ET)            # after-close 8-K
    filed = date(2026, 10, 7)
    assert not visible(filed, accepted, AsOf.parse("2026-10-07T16:00"))
    assert visible(filed, accepted, AsOf.parse("2026-10-08T09:40"))
    assert visible(filed, accepted, AsOf.parse("2026-10-07"))      # date cutoff: end of day
    # unknown time of day: counted from the next day
    assert not visible(filed, None, AsOf.parse("2026-10-07T15:00"))


def test_offline_manifest_lists_filings_without_network(tmp_path):
    sym = tmp_path / "ABC"
    sym.mkdir()
    (sym / "filings.json").write_text(json.dumps({"filings": [
        {"form": "8-K", "accession": "a2", "filing_date": "2026-10-07",
         "accepted": "2026-10-07T16:05:00"},
        {"form": "8-K", "accession": "a1", "filing_date": "2026-09-01",
         "accepted": "2026-09-01T08:00:00"},
        {"form": "10-K", "accession": "k1", "filing_date": "2026-02-01", "accepted": None}]}))

    def no_network(symbol):
        raise AssertionError("must not list filings online")

    src = SecSource("Qfii test@example.com", tmp_path, no_network)
    before = src.filings("ABC", "8-K", AsOf.parse("2026-10-07T15:50"), limit=5)
    after = src.filings("ABC", "8-K", AsOf.parse("2026-10-08T09:40"), limit=5)
    assert [f.accession_no for f in before] == ["a1"]
    assert [f.accession_no for f in after] == ["a2", "a1"]
    assert isinstance(after[0], ManifestFiling)
    assert [f.accession_no for f in src.filings("ABC", "10-K", "2026-10-07T10:00")] == ["k1"]


def test_live_listing_looks_up_acceptance_once_and_caches_it(tmp_path):
    from tests.test_sec import FakeCompany, FakeFiling, FakeObj

    class Co(FakeCompany):
        def get_filings(self, form):
            return [FakeFiling("8-K", date(2026, 10, 7), "0000000001-26-000001",
                               FakeObj(items=["Item 2.02"]))] if form == "8-K" else []

    pages = []
    src = SecSource("Qfii test@example.com", tmp_path, lambda s: Co())
    src.fetch_index = lambda acc: pages.append(acc) or (
        '<div class="infoHead">Accepted</div><div class="info">2026-10-07 16:05:00</div>')
    assert src.filings("EX", "8-K", "2026-10-07T15:50") == []
    assert len(src.filings("EX", "8-K", "2026-10-07T16:10")) == 1
    assert len(pages) == 1                                          # cached per accession
    assert len(src.filings("EX", "8-K", "2026-10-07")) == 1         # date cutoff: no lookup
