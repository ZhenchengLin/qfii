"""Assessment (TAs) and Risk & Validation agents."""

from __future__ import annotations

from importlib import resources
from typing import Any

from ...config import Config
from ...contracts import AssessmentAnswer, EvidenceItem, EvidencePacket, Proposal, RiskReport
from ...llm import make_llm
from ...portfolio import max_new_order_usd
from ...tools.client import ToolBox
from ..evidence import Collected, collect_evidence
from ..structured import fact_rows, structured_call
from ..tooluse import research_then_answer, with_tools
from .fake import fake_assessment_responder, fake_risk_responder

_PROMPTS = resources.files("qfii.agents.teams")


def _observations(packet: EvidencePacket) -> list[dict[str, Any]]:
    return [{"specialty": o.specialty, "text": o.text, "evidence_ids": o.evidence_ids}
            for o in packet.observations]


def _portfolio_view(portfolio: dict[str, Any]) -> dict[str, Any]:
    return {**{k: portfolio[k] for k in ("cash_usd", "equity_usd", "cash_pct", "holdings",
                                         "limits", "costs_bps", "tax", "benchmark")},
            "largest_allowed_order_usd": max_new_order_usd(portfolio)}


def fetchable_sources(cfg: Config, symbol: str) -> dict[str, bool]:
    """What Research can actually obtain, so Assessment only asks for fetchable evidence."""
    try:
        import edgar  # noqa: F401
        sec = bool(cfg.sec_identity)
    except ImportError:
        sec = False
    fundamentals = sec or (cfg.fundamentals_dir / f"{symbol}.json").exists()
    return {"prices_and_technicals": True, "benchmark_comparison": True, "macro": True,
            "fundamentals": fundamentals, "news_8k_filings": sec,
            "filing_text_search": sec, "market_news_headlines": False}


class AssessmentAgent:
    name = "assessment"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.llm = make_llm(cfg, self.name, fake_assessment_responder)
        self.system = _PROMPTS.joinpath("assessment.md").read_text()
        self.calls: list[dict[str, Any]] = []

    def _base(self, packet: EvidencePacket, portfolio: dict[str, Any]) -> dict[str, Any]:
        return {"symbol": packet.symbol, "as_of": packet.as_of,
                "observations": _observations(packet), "facts": fact_rows(packet.evidence),
                "quality_flags": packet.quality_flags, "gaps": packet.gaps,
                "research_can_fetch": fetchable_sources(self.cfg, packet.symbol),
                "portfolio": _portfolio_view(portfolio)}

    async def propose(self, packet: EvidencePacket, portfolio: dict[str, Any],
                      previous: Proposal | None = None,
                      feedback: list[str] | None = None) -> Proposal:
        payload = self._base(packet, portfolio)
        if previous is not None:
            payload["previous_proposal"] = previous.model_dump()
            payload["committee_feedback"] = feedback or []

        def fix(data: dict[str, Any]) -> dict[str, Any]:
            return {**data, "symbol": packet.symbol, "as_of": packet.as_of}

        return await structured_call(self.llm, self.system, payload, Proposal,
                                     {e.id for e in packet.evidence}, self.calls, fix)

    async def answer(self, packet: EvidencePacket, portfolio: dict[str, Any],
                     proposal: Proposal, questions: list[str]
                     ) -> tuple[AssessmentAnswer, list[EvidenceItem], list[dict[str, Any]]]:
        """Answer the committee, researching with the MCP tools as needed.
        Returns (answer, evidence it gathered, trace of tool calls)."""
        payload = {**self._base(packet, portfolio), "proposal": proposal.model_dump(),
                   "committee_questions": questions}
        return await with_tools(self.cfg, lambda tools: research_then_answer(
            self.llm, self.system, payload, AssessmentAnswer, tools, packet.as_of,
            {e.id for e in packet.evidence}, self.calls, self.cfg.max_tool_turns))


def impact_facts(got: Collected, proposal: Proposal, portfolio: dict[str, Any],
                 as_of: str) -> None:
    """Portfolio impact of the proposed size, computed in code, added as citable facts."""
    equity = portfolio["equity_usd"]
    size = min(equity * proposal.suggested_position_pct / 100, max_new_order_usd(portfolio))
    price_ev = next((e for e in got.evidence if e.kind == "price"), None)
    bars = (price_ev.payload.get("bars") or [])[-20:] if price_ev else []
    adv = sum(b["close"] * b["volume"] for b in bars) / len(bars) if bars else 0.0
    bps = portfolio["costs_bps"]["commission"] + portfolio["costs_bps"]["slippage"]
    held = sum(h["market_value_usd"] for h in portfolio["holdings"]
               if h["symbol"] == proposal.symbol)
    rows = {
        "proposed_order_usd": round(size, 2),
        "position_weight_after_pct": round((held + size) / equity * 100, 3) if equity else None,
        "cash_after_usd": round(portfolio["cash_usd"] - size, 2),
        "cash_after_pct": round((portfolio["cash_usd"] - size) / equity * 100, 3)
        if equity else None,
        "order_pct_of_avg_daily_volume": round(size / adv * 100, 6) if adv else None,
        "est_round_trip_cost_usd": round(2 * size * bps / 10_000, 2),
    }
    for name, value in rows.items():
        if value is None:
            continue
        got.add(symbol=proposal.symbol, kind="calc", source="code:portfolio_impact",
                as_of=as_of, retrieved=as_of, specialty="technical", name=name, value=value,
                payload={"name": name, "value": value, "label": name.replace("_", " ")})


class RiskAgent:
    name = "risk"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.llm = make_llm(cfg, self.name, fake_risk_responder)
        self.system = _PROMPTS.joinpath("risk.md").read_text()
        self.calls: list[dict[str, Any]] = []
        self.evidence: list[EvidenceItem] = []   # fresh evidence it collected
        self.trace: list[dict[str, Any]] = []     # tools it chose to call

    async def review(self, packet: EvidencePacket, portfolio: dict[str, Any],
                     proposal: Proposal, questions: list[str]) -> RiskReport:
        from ..evidence import DEFAULT_FILING_QUERIES

        queries = [q.split("] ", 1)[-1][:300] for q in questions] + list(DEFAULT_FILING_QUERIES)
        async with ToolBox(self.cfg) as tools:
            got = await collect_evidence(tools, packet.symbol, packet.as_of, queries,
                                         per_query=3)
        impact_facts(got, proposal, portfolio, packet.as_of)
        self.evidence = got.evidence
        known = {e.id for e in packet.evidence} | {e.id for e in got.evidence}
        payload = {"symbol": packet.symbol, "as_of": packet.as_of,
                   "proposal": proposal.model_dump(), "committee_questions": questions,
                   "facts": fact_rows(got.evidence), "quality_flags": got.flags,
                   "portfolio": _portfolio_view(portfolio)}

        def fix(data: dict[str, Any]) -> dict[str, Any]:
            return {**data, "symbol": packet.symbol, "as_of": packet.as_of}

        report, more, self.trace = await with_tools(self.cfg, lambda tools: research_then_answer(
            self.llm, self.system, payload, RiskReport, tools, packet.as_of, known,
            self.calls, self.cfg.max_tool_turns, fix))
        self.evidence = got.evidence + more
        return report
