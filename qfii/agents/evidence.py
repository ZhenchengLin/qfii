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
        label = LABELS.get(name) or extra.pop("label", None) or name.replace("_", " ")
        self.facts.append({"evidence_id": ev.id, "specialty": specialty, "name": name,
                           "label": label, "value": value, **extra})
        return ev


async def collect_evidence(tools: ToolBox, sym: str, as_of: str) -> Collected:
    out = Collected()
    quality = await tools.call("check_data_quality", symbol=sym, as_of=as_of)
    ind = await tools.call("compute_indicators", symbol=sym, as_of=as_of)
    hist = await tools.call("get_price_history", symbol=sym, as_of=as_of)
    bench = await tools.call("compare_to_benchmark", symbol=sym, as_of=as_of)
    macro = await tools.call("get_macro_snapshot", as_of=as_of)
    fund = await tools.call("get_fundamentals", symbol=sym, as_of=as_of)

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

    # fundamental: SEC filings public by as_of
    if fund.get("available"):
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
    flags.append("no_news")
    gaps.append("news tools not connected")
    return out
