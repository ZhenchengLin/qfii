"""Research agent workflow (LangGraph).

    START -> gather -> write_notes -> (valid?) -> assemble -> END
                           ^   |  invalid, retries left
                           +---+
                               |  invalid, no retries -> fail -> END

`gather` calls MCP tools only (deterministic numbers). `write_notes` is the single
LLM step; its JSON is validated in code, and every citation must resolve to an
evidence item produced by `gather`.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from importlib import resources
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from ...contracts import EvidenceItem, EvidencePacket, ResearchNotes, ResearchRequest
from ...llm import LLM, LLMError, failed_call, parse_json_object
from ...tools.client import ToolBox
from ...tools.indicators import LABELS

MAX_ATTEMPTS = 2
SYSTEM_PROMPT = resources.files("qfii.agents.research").joinpath("prompt.md").read_text()
NOTES_SCHEMA = ResearchNotes.model_json_schema()


class ResearchState(TypedDict, total=False):
    request: ResearchRequest
    as_of: str
    evidence: list[EvidenceItem]
    flags: list[str]
    gaps: list[str]
    facts: list[dict[str, Any]]
    notes: ResearchNotes | None
    feedback: str | None
    attempts: int
    llm_calls: list[dict[str, Any]]
    packet: EvidencePacket | None
    error: str | None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def build_research_graph(tools: ToolBox, llm: LLM):
    async def gather(state: ResearchState) -> dict:
        req = state["request"]
        sym = req.symbol
        as_of = (req.as_of or date.today()).isoformat()
        quality = await tools.call("check_data_quality", symbol=sym, as_of=as_of)
        ind = await tools.call("compute_indicators", symbol=sym, as_of=as_of)
        hist = await tools.call("get_price_history", symbol=sym, as_of=as_of)
        bench = await tools.call("compare_to_benchmark", symbol=sym, as_of=as_of)
        macro = await tools.call("get_macro_snapshot", as_of=as_of)
        fund = await tools.call("get_fundamentals", symbol=sym, as_of=as_of)
        retrieved = _now()

        evidence: list[EvidenceItem] = []
        facts: list[dict[str, Any]] = []
        gaps: list[str] = []
        flags = list(quality["flags"])

        def add(kind, source, specialty, name, value, payload, params=None, symbol=sym,
                **extra):
            ev = EvidenceItem.make(symbol=symbol, kind=kind, source=source, as_of=as_of,
                                   retrieved_at=retrieved, payload=payload, params=params)
            evidence.append(ev)
            label = LABELS.get(name) or extra.pop("label", None) or name.replace("_", " ")
            facts.append({"evidence_id": ev.id, "specialty": specialty, "name": name,
                          "label": label, "value": value, **extra})

        # technical: price, indicators, benchmark
        if hist["n"]:
            bars = hist["bars"]
            add("price", f"mcp:get_price_history/{hist['source']}", "technical", "last_close",
                bars[-1]["close"],
                {"n": hist["n"], "first_date": bars[0]["date"], "last_date": bars[-1]["date"],
                 "last_close": bars[-1]["close"], "bars": bars},
                {"last_n": len(bars)}, date=bars[-1]["date"])
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
        return {"as_of": as_of, "evidence": evidence, "facts": facts, "flags": flags,
                "gaps": gaps, "attempts": 0, "llm_calls": [], "feedback": None}

    async def write_notes(state: ResearchState) -> dict:
        req = state["request"]
        user = {
            "symbol": req.symbol, "as_of": state["as_of"], "question": req.question,
            "quality_flags": state["flags"], "facts": state["facts"],
            "known_gaps": state["gaps"],
        }
        if state.get("feedback"):
            user["your_previous_reply_was_rejected_because"] = state["feedback"]
        attempts = state["attempts"] + 1
        calls = list(state["llm_calls"])
        try:
            result = await llm.complete(SYSTEM_PROMPT, json.dumps(user, separators=(",", ":")),
                                        NOTES_SCHEMA)
        except LLMError as exc:
            calls.append(failed_call(llm, str(exc)))
            return {"attempts": attempts, "llm_calls": calls, "notes": None,
                    "feedback": str(exc)}
        call = result.record()
        try:
            notes = ResearchNotes.model_validate(parse_json_object(result.text))
            known = {e.id for e in state["evidence"]}
            bad = sorted({i for o in notes.observations for i in o.evidence_ids} - known)
            if bad:
                raise ValueError(f"cited evidence ids that do not exist: {bad}")
        except (ValueError, ValidationError) as exc:
            call.update(ok=False, error=f"invalid reply: {exc}"[:500])
            calls.append(call)
            return {"attempts": attempts, "llm_calls": calls, "notes": None,
                    "feedback": str(exc)[:500]}
        calls.append(call)
        return {"attempts": attempts, "llm_calls": calls, "notes": notes, "feedback": None}

    def route(state: ResearchState) -> str:
        if state.get("notes") is not None:
            return "assemble"
        return "write_notes" if state["attempts"] < MAX_ATTEMPTS else "fail"

    def assemble(state: ResearchState) -> dict:
        notes = state["notes"]
        assert notes is not None
        packet = EvidencePacket(
            symbol=state["request"].symbol, as_of=state["as_of"],
            observations=notes.observations, evidence=state["evidence"],
            quality_flags=state["flags"], gaps=list(dict.fromkeys(state["gaps"] + notes.gaps)),
            model=f"{llm.provider}/{llm.model}", created_at=_now(),
        )
        return {"packet": packet, "error": None}

    def fail(state: ResearchState) -> dict:
        return {"packet": None,
                "error": f"research LLM gave no valid notes after {state['attempts']} "
                         f"attempts: {state.get('feedback')}"}

    g = StateGraph(ResearchState)
    g.add_node("gather", gather)
    g.add_node("write_notes", write_notes)
    g.add_node("assemble", assemble)
    g.add_node("fail", fail)
    g.add_edge(START, "gather")
    g.add_edge("gather", "write_notes")
    g.add_conditional_edges("write_notes", route,
                            {"assemble": "assemble", "write_notes": "write_notes", "fail": "fail"})
    g.add_edge("assemble", END)
    g.add_edge("fail", END)
    return g.compile()
