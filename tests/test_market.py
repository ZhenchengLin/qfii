from datetime import date

import pytest

from qfii.tools.market import Bar, DataError, load_prices, synthetic_prices
from qfii.tools.quality import quality_flags


def test_synthetic_is_deterministic_and_has_no_lookahead(tmp_path):
    a = load_prices("synthetic", tmp_path, "AAPL", date(2026, 10, 9))
    b = load_prices("synthetic", tmp_path, "AAPL", date(2026, 6, 1))
    assert a[-1].date <= "2026-10-09" and b[-1].date <= "2026-06-01"
    # the earlier cutoff sees exactly the same past
    overlap = {x.date: x.close for x in a}
    assert all(overlap[x.date] == x.close for x in b if x.date in overlap)
    assert synthetic_prices("AAPL", date(2026, 10, 9)) == synthetic_prices("AAPL", date(2026, 10, 9))
    assert synthetic_prices("AAPL", date(2026, 10, 9)) != synthetic_prices("MSFT", date(2026, 10, 9))


def test_csv_source_truncates_at_as_of(tmp_path):
    (tmp_path / "XYZ.csv").write_text("date,close,volume\n2026-10-08,10,1\n2026-10-09,11,1\n"
                                      "2026-10-12,12,1\n")
    bars = load_prices("csv", tmp_path, "XYZ", date(2026, 10, 9))
    assert [b.date for b in bars] == ["2026-10-08", "2026-10-09"]


def test_csv_missing_file(tmp_path):
    with pytest.raises(DataError):
        load_prices("csv", tmp_path, "NOPE", date(2026, 10, 9))


def test_quality_flags():
    bars = [Bar("2026-09-01", 10, 1), Bar("2026-09-02", 0, 1)]
    flags = quality_flags(bars, date(2026, 10, 9), source="csv", min_observations=200,
                          stale_after_days=5)
    assert flags == ["stale_data", "short_sample", "missing_values"]
    assert "synthetic_data" in quality_flags(bars, date(2026, 9, 2), source="synthetic",
                                             min_observations=1, stale_after_days=5)
    assert quality_flags([], date(2026, 10, 9), source="csv", min_observations=1,
                         stale_after_days=5) == ["short_sample", "missing_values"]
