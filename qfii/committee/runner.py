"""Run one paper end to end: research -> team reports -> committee debate.

Assessment and Risk & Validation are not built yet, so the proposal and any
Risk report the committee commissions are clearly-labelled placeholders.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..agents.research.agent import ResearchAgent
from ..config import Config
from ..contracts import EvidencePacket, ResearchRequest
from ..portfolio import snapshot
from ..store import save_debate, save_packet, save_proposal
from .engine import DebateResult, Dossier, run_debate
from .members import build_members
from .memory import recall_all


class PlaceholderTeams:
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
    proposal = {"status": "placeholder: Assessment agent not built",
                "symbol": packet.symbol, "observations": facts, "gaps": packet.gaps}
    return proposal


@dataclass
class PaperRun:
    packet: EvidencePacket
    debate: DebateResult
    paper_id: int | None
    debate_id: int | None


async def run_paper(cfg: Config, conn: sqlite3.Connection, symbol: str,
                    as_of: date | None = None, save: bool = False,
                    on_research=None, on_round=None, on_ballot=None) -> PaperRun:
    as_of = as_of or date.today()
    request = ResearchRequest(symbol=symbol, as_of=as_of, requested_by="user")
    research = ResearchAgent(cfg)
    packet, research_calls = await research.run(request)
    if on_research:
        on_research(packet)
    proposal = placeholder_proposal(packet)
    dossier = Dossier(packet.symbol, proposal, None,
                      snapshot(conn, cfg, as_of).model_dump(mode="json"))
    members = build_members(cfg)
    result = await run_debate(dossier, members, PlaceholderTeams(), cfg.committee,
                              recall_all(conn, cfg.committee.memory_items), on_round,
                              on_ballot)
    paper_id = debate_id = None
    if save:
        paper_id = save_packet(conn, request, packet, research_calls,
                               cfg.agents["research"].tier)
        proposal_id = save_proposal(conn, paper_id, proposal, "assessment:placeholder")
        debate_id = save_debate(conn, paper_id, packet.symbol, result,
                                {m.name: (cfg.agents[m.name].tier, m.calls) for m in members},
                                proposal_id)
    return PaperRun(packet, result, paper_id, debate_id)
