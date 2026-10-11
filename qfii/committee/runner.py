"""Run one paper end to end, the way the department works:

  Research packet -> Assessment proposal
     verdict reject / defer / needs_evidence: the paper stops there
     verdict submit: committee debate; Risk & Validation reports only when the
       committee commissions it; Assessment answers the committee's questions
     committee sends it back: Assessment revises with the feedback and the
       committee debates again, at most committee.max_send_backs times
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..agents.research.agent import ResearchAgent
from ..agents.structured import AgentFailed
from ..agents.teams.agents import AssessmentAgent, RiskAgent
from ..config import Config
from ..contracts import EvidencePacket, Proposal, ResearchRequest
from ..papers import transition
from ..portfolio import snapshot
from ..store import record_llm_calls, save_debate, save_packet, save_proposal
from .engine import DebateResult, Dossier, run_debate
from .members import build_members
from .memory import recall_all


class PlaceholderTeams:
    """Used by the benchmark, which keeps the dossier fixed across models."""

    async def report(self, team: str, requests: list[str], dossier: Dossier) -> dict[str, Any]:
        return {"status": "placeholder", "team": team,
                "note": f"{team} agent not built yet; no new analysis available",
                "answering": requests}


def placeholder_proposal(packet: EvidencePacket) -> dict[str, Any]:
    by_id = {e.id: e for e in packet.evidence}
    facts = [{"text": o.text, "specialty": o.specialty,
              "evidence": [{"id": i, **{k: v for k, v in by_id[i].payload.items() if k != "bars"}}
                           for i in o.evidence_ids]}
             for o in packet.observations]
    return {"status": "placeholder: Assessment agent not built",
            "symbol": packet.symbol, "observations": facts, "gaps": packet.gaps}


class Teams:
    """Assessment and Risk & Validation answering the committee through reports."""

    def __init__(self, cfg: Config, packet: EvidencePacket, portfolio: dict[str, Any],
                 assessment: AssessmentAgent, proposal: Proposal):
        self.packet, self.portfolio, self.proposal = packet, portfolio, proposal
        self.assessment = assessment
        self.risk = RiskAgent(cfg)

    async def report(self, team: str, requests: list[str], dossier: Dossier) -> dict[str, Any]:
        try:
            if team == "risk":
                rep = await self.risk.review(self.packet, self.portfolio, self.proposal,
                                             requests)
                evidence, trace = self.risk.evidence, self.risk.trace
            else:
                rep, evidence, trace = await self.assessment.answer(
                    self.packet, self.portfolio, self.proposal, requests)
        except AgentFailed as exc:
            return {"team": team, "error": str(exc)}
        # what the team looked up or calculated, so the committee can see the work
        looked_up = {e.id: e for e in evidence}
        return {"team": team, **rep.model_dump(exclude_none=True),
                "tool_trace": [{**t, "label": looked_up[t["evidence_id"]].payload.get("label")}
                               if t.get("evidence_id") in looked_up else t for t in trace]}


@dataclass
class PaperRun:
    packet: EvidencePacket
    proposal: Proposal | None
    debates: list[DebateResult] = field(default_factory=list)
    paper_id: int | None = None
    debate_id: int | None = None
    outcome: str = ""

    @property
    def debate(self) -> DebateResult | None:
        return self.debates[-1] if self.debates else None


async def run_paper(cfg: Config, conn: sqlite3.Connection, symbol: str,
                    as_of: date | None = None, save: bool = False,
                    on_research=None, on_round=None, on_ballot=None,
                    on_proposal=None) -> PaperRun:
    as_of = as_of or date.today()
    request = ResearchRequest(symbol=symbol, as_of=as_of, requested_by="user")
    packet, research_calls = await ResearchAgent(cfg).run(request)
    if on_research:
        on_research(packet)
    portfolio = snapshot(conn, cfg, as_of).model_dump(mode="json")
    paper_id = (save_packet(conn, request, packet, research_calls, cfg.agents["research"].tier)
                if save else None)

    assessment = AssessmentAgent(cfg)

    def persist_calls(agent: str, calls: list[dict[str, Any]]) -> None:
        if save and calls:
            with conn:
                record_llm_calls(conn, agent, cfg.agents[agent].tier, calls, paper_id)
            calls.clear()

    try:
        proposal = await assessment.propose(packet, portfolio)
    except AgentFailed as exc:
        persist_calls("assessment", exc.calls)
        if save:
            with conn:
                transition(conn, paper_id, "deferred", "assessment", str(exc)[:500])
        return PaperRun(packet, None, paper_id=paper_id, outcome="assessment_failed")
    persist_calls("assessment", assessment.calls)
    if on_proposal:
        on_proposal(proposal, 1)
    proposal_id = (save_proposal(conn, paper_id, proposal.model_dump(), verdict=proposal.verdict)
                   if save else None)
    if proposal.verdict != "submit":
        if save and proposal.verdict == "needs_evidence":
            from ..discovery import enqueue_request

            for question in proposal.research_questions or ["missing evidence (unspecified)"]:
                enqueue_request(conn, packet.symbol, question, "assessment", paper_id)
        return PaperRun(packet, proposal, paper_id=paper_id,
                        outcome=f"assessment_{proposal.verdict}")

    run = PaperRun(packet, proposal, paper_id=paper_id)
    risk_report = None
    for attempt in range(cfg.committee.max_send_backs + 1):
        teams = Teams(cfg, packet, portfolio, assessment, proposal)
        members = build_members(cfg)
        dossier = Dossier(packet.symbol, proposal.model_dump(), risk_report, portfolio)
        result = await run_debate(dossier, members, teams, cfg.committee,
                                  recall_all(conn, cfg.committee.memory_items), on_round,
                                  on_ballot)
        run.debates.append(result)
        persist_calls("assessment", assessment.calls)
        persist_calls("risk", teams.risk.calls)
        if save:
            run.debate_id = save_debate(
                conn, paper_id, packet.symbol, result,
                {m.name: (cfg.agents[m.name].tier, m.calls) for m in members}, proposal_id)
        if result.outcome != "send_back":
            break
        if attempt == cfg.committee.max_send_backs:
            if save:
                with conn:
                    transition(conn, paper_id, "shelved", "committee",
                               "send-back limit reached")
            run.outcome = "shelved_after_send_back"
            return run
        feedback = result.final.requests
        try:
            if "assessment" in result.final.teams:
                proposal = await assessment.propose(packet, portfolio, proposal, feedback)
            if "risk" in result.final.teams:
                rep = await RiskAgent(cfg).review(packet, portfolio, proposal, feedback)
                risk_report = {"team": "risk", **rep.model_dump(exclude_none=True)}
        except AgentFailed as exc:
            persist_calls("assessment", exc.calls)
            if save:
                with conn:
                    transition(conn, paper_id, "shelved", "assessment", str(exc)[:500])
            run.outcome = "shelved_revision_failed"
            return run
        persist_calls("assessment", assessment.calls)
        if on_proposal:
            on_proposal(proposal, attempt + 2)
        if save:
            proposal_id = save_proposal(conn, paper_id, proposal.model_dump(),
                                        "assessment:revision", "submit")
        run.proposal = proposal
    run.outcome = run.debates[-1].outcome
    return run
