import json
import shutil
from datetime import date
from pathlib import Path

import httpx
import pytest

from qfii.data_fetch import (
    FetchError,
    fetch_fred_series,
    fetch_yahoo_prices,
    write_prices,
    write_series,
)
from qfii.tools.fundamentals import fundamentals
from qfii.tools.indicators import benchmark_stats
from qfii.tools.macro import macro_snapshot, publication_lag_days, series_snapshot
from qfii.tools.market import load_prices

FIXTURES = Path(__file__).parent / "fixtures"


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def yahoo_payload():
    # 2026-10-08, -09, -10 at 09:30 EDT; the middle day is a hole
    return {"chart": {"error": None, "result": [{
        "meta": {"gmtoffset": -14400},
        "timestamp": [1791466200, 1791552600, 1791639000],
        "indicators": {"quote": [{"close": [10.0, None, 12.0], "volume": [100, 0, 300]}],
                       "adjclose": [{"adjclose": [9.5, None, 11.5]}]}}]}}


def test_yahoo_uses_adjusted_close_and_skips_holes():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        return httpx.Response(200, json=yahoo_payload())

    bars = fetch_yahoo_prices(client(handler), "AAPL")
    assert "chart/AAPL" in seen["url"] and "interval=1d" in seen["url"]
    assert [(b.date, b.close, b.volume) for b in bars] == [("2026-10-08", 9.5, 100.0),
                                                           ("2026-10-10", 11.5, 300.0)]


def test_yahoo_error_raises():
    body = {"chart": {"result": None, "error": {"code": "Not Found"}}}
    with pytest.raises(FetchError, match="Not Found"):
        fetch_yahoo_prices(client(lambda r: httpx.Response(200, json=body)), "ZZZZ")
    with pytest.raises(FetchError, match="429"):
        fetch_yahoo_prices(client(lambda r: httpx.Response(429)), "AAPL")


def test_write_prices_merges_and_loads(tmp_path):
    bars = fetch_yahoo_prices(client(lambda r: httpx.Response(200, json=yahoo_payload())), "X")
    write_prices(tmp_path, "X", bars)
    write_prices(tmp_path, "X", bars[-1:])  # re-download overlapping day
    loaded = load_prices("csv", tmp_path, "X", date(2026, 10, 9))
    assert [b.date for b in loaded] == ["2026-10-08"]  # no look-ahead past as_of


def test_fred_parses_and_skips_missing(tmp_path):
    text = "observation_date,DGS10\n2026-10-06,4.10\n2026-10-07,.\n2026-10-08,4.20\n"
    rows = fetch_fred_series(client(lambda r: httpx.Response(200, text=text)), "DGS10")
    assert rows == [("2026-10-06", 4.10), ("2026-10-08", 4.20)]
    write_series(tmp_path, "DGS10", rows)
    snap = macro_snapshot(tmp_path, ["DGS10", "NOPE"], date(2026, 10, 9))
    assert snap["DGS10"]["value"] == 4.20 and snap["DGS10"]["label"].startswith("10-year")
    assert snap["NOPE"]["available"] is False


def test_monthly_series_respects_publication_lag():
    rows = [(date(2025, m, 1), float(m)) for m in range(1, 13)]
    rows += [(date(2026, m, 1), 100.0 + m) for m in range(1, 10)]
    assert publication_lag_days(rows) == 45
    snap = series_snapshot(rows, date(2026, 10, 9))
    # September's figure (dated 09-01) is not public until mid-October -> August is latest
    assert snap["observation_date"] == "2026-08-01" and snap["value"] == 108.0
    assert snap["value_12m_earlier"] == 8.0


def test_daily_series_lag_is_one_day():
    rows = [(date(2026, 10, d), float(d)) for d in range(1, 10)]
    assert series_snapshot(rows, date(2026, 10, 9))["observation_date"] == "2026-10-08"


def test_fundamentals_only_use_filings_public_by_as_of():
    facts = json.loads((FIXTURES / "companyfacts_example.json").read_text())
    before = fundamentals(facts, date(2025, 10, 1))   # FY2025 10-K not yet filed
    assert before["metrics"]["revenue"]["value"] == 100e9
    assert before["metrics"]["total_assets"]["value"] == 300e9  # from the 10-Q
    after = fundamentals(facts, date(2025, 11, 15))
    rev = after["metrics"]["revenue"]
    assert (rev["value"], rev["prior_year_value"]) == (120e9, 100e9)  # restatement not yet filed
    assert after["derived"]["revenue_growth_yoy"] == pytest.approx(0.2)
    assert after["derived"]["net_margin"] == pytest.approx(0.25)
    assert after["derived"]["debt_to_equity"] == pytest.approx(0.5)
    assert after["metrics"]["eps_diluted"]["value"] == 6.5
    restated = fundamentals(facts, date(2026, 2, 1))
    assert restated["metrics"]["revenue"]["prior_year_value"] == 101e9
    assert fundamentals(facts, date(2024, 1, 1))["available"] is False


def test_benchmark_stats():
    import math
    days = [f"d{i:03d}" for i in range(300)]
    log_b = [0.0]
    for i in range(1, 300):
        log_b.append(log_b[-1] + (0.01 if i % 3 else -0.015))
    bench = [(d, 100 * math.exp(x)) for d, x in zip(days, log_b)]
    double = [(d, 50 * math.exp(2 * x)) for d, x in zip(days, log_b)]  # exactly 2x log returns
    stats = benchmark_stats(double, bench)
    assert stats["beta_12m"] == pytest.approx(2.0)
    assert stats["correlation_12m"] == pytest.approx(1.0)
    b3 = math.exp(log_b[-1] - log_b[-64]) - 1
    s3 = math.exp(2 * (log_b[-1] - log_b[-64])) - 1
    assert stats["excess_return_3m"] == pytest.approx(s3 - b3, abs=1e-6)
    assert benchmark_stats(double[:10], bench)["excess_return_3m"] is None


async def test_research_uses_fundamentals_and_macro_when_present(cfg):
    from qfii.agents.research.agent import ResearchAgent
    from qfii.contracts import ResearchRequest

    cfg.fundamentals_dir.mkdir(parents=True)
    shutil.copy(FIXTURES / "companyfacts_example.json", cfg.fundamentals_dir / "AAPL.json")
    write_series(cfg.macro_dir, "DGS10", [(f"2026-10-0{d}", 4.0 + d / 100) for d in range(1, 9)])
    packet, _ = await ResearchAgent(cfg).run(ResearchRequest(symbol="AAPL",
                                                             as_of=date(2026, 10, 9)))
    specialties = {o.specialty for o in packet.observations}
    assert specialties == {"technical", "fundamental", "macro_news"}
    assert "no_fundamentals" not in packet.quality_flags
    assert "no_macro" not in packet.quality_flags and "no_news" in packet.quality_flags
    kinds = {e.kind for e in packet.evidence}
    assert kinds == {"price", "calc", "filing", "macro"}
    assert any("excess_return_12m" == e.payload.get("name") for e in packet.evidence)
