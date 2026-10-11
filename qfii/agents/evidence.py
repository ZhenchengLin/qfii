"""Evidence gathering shared by Research and Risk & Validation.

Calls the MCP tools for one symbol and turns every number into an EvidenceItem
with a stable id and a fixed label. Both agents see the same kinds of facts, so
Risk can re-check a proposal against exactly what Research would find.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..contracts import EvidenceItem
from ..tools.client import ToolBox
from ..tools.indicators import LABELS


@dataclass
class Collected:
    evidence: list[EvidenceItem] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    def add(self, *, symbol: str | None, kind: str, source: str, as_of: str, retrieved: str,
            specialty: str, name: str, value: Any, payload: dict[str, Any],
            params: dict[str, Any] | None = None, **extra: Any) -> EvidenceItem:
        ev = EvidenceItem.make(symbol=symbol, kind=kind, source=source, as_of=as_of,
                               retrieved_at=retrieved, payload=payload, params=params)
        self.evidence.append(ev)
        label = (LABELS.get(name) or extra.pop("label", None) or payload.get("label")
                 or name.replace("_", " "))
        self.facts.append({"evidence_id": ev.id, "specialty": specialty, "name": name,
                           "label": label, "value": value, **extra})
        return ev


def valuation_metrics(close: float, annual: dict) -> dict[str, float]:
    """Trailing valuation from the last close and the latest 10-K, computed in code.
    Market cap uses 10-K diluted shares (weighted average for the fiscal year)."""
    shares = annual.get("diluted_shares")
    if not close or not shares:
        return {}
    cap = close * shares
    out = {"market_cap": round(cap, 2)}
    if annual.get("net_income") and annual["net_income"] > 0:
        out["pe_trailing_fy"] = round(cap / annual["net_income"], 4)
    if annual.get("revenue"):
        out["price_to_sales_fy"] = round(cap / annual["revenue"], 4)
    if annual.get("free_cash_flow") is not None:
        out["fcf_yield_fy"] = round(annual["free_cash_flow"] / cap, 6)
    if annual.get("stockholders_equity"):
        out["price_to_book_fy"] = round(cap / annual["stockholders_equity"], 4)
    return out


VALUATION_LABELS = {
    "market_cap": "market capitalization (last close x 10-K diluted shares)",
    "pe_trailing_fy": "price / earnings on the latest fiscal year (10-K)",
    "price_to_sales_fy": "price / sales on the latest fiscal year (10-K)",
    "fcf_yield_fy": "free-cash-flow yield on the latest fiscal year (10-K)",
    "price_to_book_fy": "price / book value at the latest fiscal year end (10-K)",
}


# Filing searches Research always runs; Risk & Validation adds the committee's questions.
DEFAULT_FILING_QUERIES = (
    "revenue growth demand outlook guidance",
    "competition regulation material risks",
    "liquidity debt cash capital expenditures",
)


async def collect_evidence(tools: ToolBox, sym: str, as_of: str,
                           queries: tuple[str, ...] | list[str] = DEFAULT_FILING_QUERIES,
                           per_query: int = 2) -> Collected:
    out = Collected()
    quality = await tools.call("check_data_quality", symbol=sym, as_of=as_of)
    ind = await tools.call("compute_indicators", symbol=sym, as_of=as_of)
    hist = await tools.call("get_price_history", symbol=sym, as_of=as_of)
    bench = await tools.call("compare_to_benchmark", symbol=sym, as_of=as_of)
    macro = await tools.call("get_macro_snapshot", as_of=as_of)
    sec_fund = await tools.call("get_sec_fundamentals", symbol=sym, as_of=as_of)
    fund = (sec_fund if sec_fund.get("available")
            else await tools.call("get_fundamentals", symbol=sym, as_of=as_of))
    news = await tools.call("get_sec_news", symbol=sym, as_of=as_of)
    hits: dict[str, dict] = {}
    for q in queries:
        res = await tools.call("search_sec_filings", symbol=sym, as_of=as_of, query=q,
                               k=per_query)
        for h in res.get("hits", []):
            hits.setdefault(h["text"], {**h, "query": q})

    retrieved = datetime.now(UTC).isoformat(timespec="seconds")
    out.flags = list(quality["flags"])
    gaps, flags = out.gaps, out.flags

    def add(kind, source, specialty, name, value, payload, params=None, symbol=sym,
            **extra):
        out.add(symbol=symbol, kind=kind, source=source, as_of=as_of, retrieved=retrieved,
                specialty=specialty, name=name, value=value, payload=payload, params=params,
                **extra)

    # technical: price, indicators, benchmark
    if hist["n"]:
        bars = hist["bars"]
        add("price", f"mcp:get_price_history/{hist['source']}", "technical", "last_close",
            bars[-1]["close"],
            {"n": hist["n"], "first_date": bars[0]["date"], "last_date": bars[-1]["date"],
             "last_close": bars[-1]["close"], "bars": bars},
            {"last_n": len(bars)}, date=bars[-1]["date"])
        tail = bars[-20:]
        adv = round(sum(b["close"] * b["volume"] for b in tail) / len(tail), 2)
        add("calc", f"mcp:get_price_history/{hist['source']}", "technical",
            "avg_daily_dollar_volume_20d", adv,
            {"name": "avg_daily_dollar_volume_20d", "value": adv}, {"window_days": len(tail)})
    for name, item in ind["indicators"].items():
        if item["value"] is None:
            gaps.append(f"{name} unavailable: not enough history")
            continue
        add("calc", f"mcp:compute_indicators/{ind['source']}", "technical", name,
            item["value"], {"name": name, "value": item["value"]},
            {"window_days": item["window"], "last_date": ind["last_date"]},
            window_days=item["window"])
    stats = {k: v for k, v in bench["stats"].items() if v is not None}
    for name, value in stats.items():
        add("calc", f"mcp:compare_to_benchmark/{bench['source']}", "technical", name, value,
            {"name": name, "value": value, "benchmark": bench["benchmark"]},
            benchmark=bench["benchmark"])
    if not stats:
        flags.append("no_benchmark")
        gaps.append(f"no overlapping history with benchmark {bench['benchmark']}")

    # fundamental: SEC filings public by as_of (EdgarTools XBRL first, companyfacts fallback)
    if fund is sec_fund:
        for key in ("annual", "quarterly"):
            block = fund.get(key)
            if not block:
                continue
            for name, value in block["metrics"].items():
                if name == "periods" or value is None:
                    continue
                add("filing", f"mcp:get_sec_fundamentals/{block['form']}", "fundamental",
                    f"{key}_{name}", value,
                    {"name": f"{key}_{name}", "value": value, "form": block["form"],
                     "filed": block["filed"], "period": block["period"], "url": block["url"],
                     "label": f"{key} {name.replace('_', ' ')} ({block['form']} for period "
                              f"ending {block['period']}, filed {block['filed']})"},
                    period_end=block["period"], filed=block["filed"])
        for name, value in fund["derived"].items():
            add("calc", "mcp:get_sec_fundamentals/derived", "fundamental", name, value,
                {"name": name, "value": value, "label": name.replace("_", " ")})
        annual = (fund.get("annual") or {})
        close = hist["bars"][-1]["close"] if hist["n"] else None
        for name, value in valuation_metrics(close, annual.get("metrics") or {}).items():
            add("calc", "code:valuation", "fundamental", name, value,
                {"name": name, "value": value, "label": VALUATION_LABELS[name],
                 "close_date": hist["bars"][-1]["date"], "filed": annual.get("filed")})
    elif fund.get("available"):
        for name, m in fund["metrics"].items():
            add("filing", f"mcp:get_fundamentals/SEC EDGAR {m['form']}", "fundamental",
                name, m["value"], {"name": name, **m}, period_end=m["period_end"],
                filed=m["filed"], prior_year_value=m.get("prior_year_value"))
        for name, value in fund["derived"].items():
            add("calc", "mcp:get_fundamentals/derived", "fundamental", name, value,
                {"name": name, "value": value})
    else:
        flags.append("no_fundamentals")
        gaps.append(f"fundamentals unavailable: {fund.get('reason', 'no data')}")

    # macro: FRED series public by as_of
    available = {k: v for k, v in macro["series"].items() if v.get("available")}
    for sid, m in available.items():
        add("macro", "mcp:get_macro_snapshot/FRED", "macro_news", sid, m["value"],
            {"series": sid, **m}, symbol=None, label=m["label"],
            observation_date=m["observation_date"], value_3m_earlier=m["value_3m_earlier"],
            value_12m_earlier=m["value_12m_earlier"])
    if not available:
        flags.append("no_macro")
        gaps.append("macro data unavailable (run `qfii data fetch --macro`)")

    # news: 8-K current reports (company-issued, primary source)
    for n in news.get("news", []):
        events = ", ".join(f"Item {i['code']} {i['label']}" for i in n["items"])
        add("news", f"mcp:get_sec_news/8-K {n['filed']}", "macro_news", "8k_event",
            n["headline"] or events,
            {"name": "8k_event", "form": n["form"], "filed": n["filed"], "items": n["items"],
             "headline": n["headline"], "text": n["text"][:3000], "url": n["url"],
             "label": f"8-K filed {n['filed']}: {events}"})
    if not news.get("available"):
        flags.append("no_news")
        gaps.append(f"news unavailable: {news.get('reason', 'not configured')}")

    # filing passages relevant to the queries (risk factors, MD&A, press releases)
    for h in hits.values():
        add("filing", f"mcp:search_sec_filings/{h['form']} {h['filed']}", "fundamental",
            "filing_passage", h["text"][:900],
            {"name": "filing_passage", "form": h["form"], "filed": h["filed"],
             "section": h["section"], "url": h["url"], "text": h["text"], "query": h["query"],
             "label": f"{h['form']} {h['section']} (filed {h['filed']}), passage"})
    return out
