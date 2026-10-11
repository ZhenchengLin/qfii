from dataclasses import dataclass, field
from datetime import date

import pytest

from qfii.tools.sec import SecSource, SecUnavailable, bm25, chunk, html_to_text


@dataclass
class FakeFinancials:
    rev: float

    def get_revenue(self): return self.rev
    def get_net_income(self): return self.rev * 0.3
    def get_operating_income(self): return self.rev * 0.4
    def get_free_cash_flow(self): raise KeyError("not reported")
    def income_statement(self): raise ValueError("no statement")


@dataclass
class FakeObj:
    rev: float = 100.0
    items: list = field(default_factory=list)
    risk_factors: str = ""
    management_discussion: str = ""

    @property
    def financials(self): return FakeFinancials(self.rev)
    def text(self): return "Company announced a new chief financial officer effective today."


@dataclass
class FakeFiling:
    form: str
    filing_date: date
    accession_no: str
    obj_: FakeObj
    attachments: list = field(default_factory=list)
    period_of_report: str = ""
    homepage_url: str = "https://sec.example/f"

    def obj(self): return self.obj_


RISK = ("Our datacenter capacity may not keep pace with AI demand, which could constrain "
        "revenue growth.\n\nCompetition in cloud services is intense and pricing pressure "
        "could reduce margins.\n\nWe rely on third-party suppliers for chips.")


class FakeCompany:
    name = "Example Corp"

    def get_filings(self, form):
        return {
            "10-K": [FakeFiling("10-K", date(2026, 7, 29), "k2", FakeObj(120, risk_factors=RISK)),
                     FakeFiling("10-K", date(2025, 7, 30), "k1", FakeObj(100, risk_factors=RISK))],
            "10-Q": [FakeFiling("10-Q", date(2026, 4, 29), "q1", FakeObj(30))],
            "8-K": [FakeFiling("8-K", date(2026, 9, 2), "e3", FakeObj(items=["Item 9.01"])),
                    FakeFiling("8-K", date(2026, 8, 1), "e2", FakeObj(items=["Item 5.02"])),
                    FakeFiling("8-K", date(2025, 1, 1), "e1", FakeObj(items=["Item 2.02"]))],
        }[form]


@pytest.fixture
def src(tmp_path):
    return SecSource("Qfii test@example.com", tmp_path, lambda sym: FakeCompany())


def test_requires_identity(tmp_path):
    with pytest.raises(SecUnavailable, match="sec_user_agent"):
        SecSource("", tmp_path, lambda s: FakeCompany())


def test_fundamentals_point_in_time(src):
    now = src.fundamentals("EX", date(2026, 10, 9))
    assert now["annual"]["filed"] == "2026-07-29" and now["annual"]["metrics"]["revenue"] == 120
    assert now["derived"]["annual_net_margin"] == pytest.approx(0.3)
    assert "free_cash_flow" not in now["annual"]["metrics"]  # missing item is skipped
    before = src.fundamentals("EX", date(2026, 7, 1))
    assert before["annual"]["filed"] == "2025-07-30"   # 2026 10-K not public yet
    assert src.fundamentals("EX", date(2025, 1, 1))["available"] is False


def test_results_are_cached_per_filing(src, tmp_path):
    src.fundamentals("EX", date(2026, 10, 9))
    assert (tmp_path / "EX" / "k2.metrics.json").exists()


def test_news_window_and_exhibit_only_8ks(src):
    news = src.news("EX", date(2026, 10, 9))
    assert [n["accession"] for n in news] == ["e2"]       # e3 is exhibits only, e1 too old
    assert news[0]["items"][0]["label"] == "director or officer change"
    assert "chief financial officer" in news[0]["text"]
    assert src.news("EX", date(2026, 8, 15))[0]["accession"] == "e2"
    assert src.news("EX", date(2026, 7, 31)) == []         # e2 not filed yet


def test_search_finds_relevant_passage(src):
    hits = src.search("EX", date(2026, 10, 9), "chips suppliers", k=2)
    assert hits and "suppliers" in hits[0]["text"] and hits[0]["form"] == "10-K"
    assert hits[0]["filed"] == "2026-07-29"


def test_chunk_and_bm25():
    parts = chunk("A" * 50 + ".\n\n" + "word " * 400, size=300)
    assert all(len(p) <= 300 for p in parts) and len(parts) >= 4
    docs = [{"text": "interest rate risk on debt"}, {"text": "cloud revenue growth"}]
    assert bm25(docs, "revenue growth")[0]["text"] == "cloud revenue growth"
    assert bm25(docs, "the and of") == []


def test_html_to_text_strips_markup():
    text = html_to_text("<html><body><h1>Results</h1><p>Revenue was $90&nbsp;billion</p>"
                        "<script>x()</script></body></html>")
    assert "Results" in text and "Revenue was $90" in text and "x()" not in text


def test_valuation_metrics_from_close_and_10k():
    from qfii.agents.evidence import valuation_metrics

    annual = {"diluted_shares": 1_000, "net_income": 5_000, "revenue": 20_000,
              "free_cash_flow": 2_500, "stockholders_equity": 10_000}
    v = valuation_metrics(100.0, annual)
    assert v == {"market_cap": 100_000, "pe_trailing_fy": 20.0, "price_to_sales_fy": 5.0,
                 "fcf_yield_fy": 0.025, "price_to_book_fy": 10.0}
    assert "pe_trailing_fy" not in valuation_metrics(100.0, {**annual, "net_income": -1})
    assert valuation_metrics(100.0, {"revenue": 1}) == {}


def test_repeated_searches_do_not_refetch(src):
    calls = {"n": 0}
    company = FakeCompany()
    real = company.get_filings

    def counting(form):
        calls["n"] += 1
        return real(form)

    company.get_filings = counting
    src._companies["EX"] = company
    for q in ("chips", "competition", "datacenter", "pricing", "suppliers"):
        src.search("EX", date(2026, 10, 9), q)
    assert calls["n"] == 3   # 10-K, 10-Q, 8-K listed once each, not once per query
