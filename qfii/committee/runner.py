"""Run one paper end to end: research -> team reports -> committee debate.

Assessment and Risk & Validation are not built yet (waiting on Ben's report
formats), so their reports are clearly-labelled placeholders built from the
research packet.
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
from ..store import save_debate, save_packet
from .engine import DebateResult, Dossier, run_debate
from .members import build_members
from .memory import recall_all


class PlaceholderTeams:
    async def report(self, team: str, requests: list[str], dossier: Dossier) -> dict[str, Any]:
        return {"status": "placeholder", "team": team,
                "note": f"{team} agent not built yet; no new analysis available",
                "answering": requests}


def placeholder_reports(packet: EvidencePacket) -> tuple[dict[str, Any], dict[str, Any]]:
    facts = [o.text for o in packet.observations]
    proposal = {"status": "placeholder: Assessment agent not built",
                "symbol": packet.symbol, "evidence": facts, "gaps": packet.gaps}
    risk = {"status": "placeholder: Risk & Validation agent not built",
            "quality_flags": packet.quality_flags}
    return proposal, risk


@dataclass
class PaperRun:
    packet: EvidencePacket
    debate: DebateResult
    paper_id: int | None
    debate_id: int | None


async def run_paper(cfg: Config, conn: sqlite3.Connection, symbol: str,
                    as_of: date | None = None, save: bool = False) -> PaperRun:
    as_of = as_of or date.today()
    request = ResearchRequest(symbol=symbol, as_of=as_of, requested_by="user")
    research = ResearchAgent(cfg)
    packet, research_calls = await research.run(request)
    proposal, risk = placeholder_reports(packet)
    dossier = Dossier(packet.symbol, proposal, risk,
                      snapshot(conn, cfg, as_of).model_dump(mode="json"))
    members = build_members(cfg)
    result = await run_debate(dossier, members, PlaceholderTeams(), cfg.committee,
                              recall_all(conn, cfg.committee.memory_items))
    paper_id = debate_id = None
    if save:
        paper_id = save_packet(conn, request, packet, research_calls,
                               cfg.agents["research"].tier)
        debate_id = save_debate(conn, paper_id, packet.symbol, result,
                                {m.name: (cfg.agents[m.name].tier, m.calls) for m in members})
    return PaperRun(packet, result, paper_id, debate_id)
