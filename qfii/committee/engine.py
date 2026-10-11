"""Committee debate loop.

Each round all three members see the same view (reports, portfolio, every
earlier round's arguments and ballots, and ONLY their own memory) and vote at
the same time, so nobody anchors on another member's ballot within a round.

    round r: ballots <- 3 members in parallel
             outcome <- tally (deterministic)
             invest / replace / pass / send_back  -> debate ends
             request_info -> teams write reports everyone sees -> next round
             continue     -> next round
    after max_rounds without a decision -> no_consensus (shelve, move on)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from ..config import COMMITTEE, Committee
from ..contracts import Ballot
from .protocol import RoundOutcome, tally

FINAL_OUTCOMES = ("invest", "replace", "pass", "send_back")


@dataclass
class Dossier:
    """Everything the committee debates over. Built once per debate."""

    symbol: str
    proposal: dict[str, Any]       # Assessment report
    risk_report: dict[str, Any]    # Risk & Validation report
    portfolio: dict[str, Any]      # PortfolioSnapshot as JSON


@dataclass
class DebateView:
    member: str
    round: int
    max_rounds: int
    dossier: Dossier
    info_reports: list[dict[str, Any]]
    transcript: list[dict[str, Any]]
    memory: list[str]
    rules: dict[str, Any]

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            "you_are": self.member, "round": self.round, "max_rounds": self.max_rounds,
            "final_round": self.round == self.max_rounds, "rules": self.rules,
            "symbol": self.dossier.symbol, "assessment_proposal": self.dossier.proposal,
            "risk_report": self.dossier.risk_report, "portfolio": self.dossier.portfolio,
            "additional_reports": self.info_reports, "previous_rounds": self.transcript,
            "your_memory": self.memory,
        }


class MemberAgent(Protocol):
    name: str

    async def deliberate(self, view: DebateView) -> Ballot: ...


class Teams(Protocol):
    async def report(self, team: str, requests: list[str], dossier: Dossier) -> dict[str, Any]: ...


@dataclass
class RoundRecord:
    round: int
    ballots: list[Ballot]
    outcome: RoundOutcome
    reports: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DebateResult:
    outcome: str                    # invest | replace | pass | send_back | no_consensus
    rounds: list[RoundRecord]
    final: RoundOutcome | None

    @property
    def size_usd(self) -> float | None:
        return self.final.size_usd if self.final else None


def _rules(rules: Committee) -> dict[str, Any]:
    return {
        "trade_rule": rules.invest_rule, "procedural_rule": rules.procedural_rule,
        "size_tolerance_pct": rules.size_tolerance_pct, "max_rounds": rules.max_rounds,
        "no_consensus_after_max_rounds": "paper is shelved; committee moves on",
    }


async def _ballot(member: MemberAgent, view: DebateView) -> Ballot:
    try:
        ballot = await member.deliberate(view)
        if ballot.member != member.name:
            raise ValueError(f"ballot signed by {ballot.member}, expected {member.name}")
        return ballot
    except (ValueError, ValidationError, RuntimeError) as exc:
        # A member that cannot produce a valid ballot passes this round; it never
        # blocks or forces a trade on its own.
        return Ballot(member=member.name, action="pass",
                      argument=f"[invalid ballot, counted as pass] {exc}"[:3000])


def _transcript_entry(rec: RoundRecord) -> dict[str, Any]:
    return {
        "round": rec.round,
        "ballots": [b.model_dump(exclude={"note_to_self"}, exclude_none=True)
                    for b in rec.ballots],
        "result": rec.outcome.kind, "detail": rec.outcome.detail,
    }


async def run_debate(dossier: Dossier, members: list[MemberAgent], teams: Teams,
                     rules: Committee, memories: dict[str, list[str]] | None = None
                     ) -> DebateResult:
    names = sorted(m.name for m in members)
    if names != sorted(COMMITTEE):
        raise ValueError(f"committee must be exactly {COMMITTEE}, got {names}")
    memories = memories or {}
    reports: list[dict[str, Any]] = []
    rounds: list[RoundRecord] = []

    for r in range(1, rules.max_rounds + 1):
        transcript = [_transcript_entry(rec) for rec in rounds]
        views = [DebateView(m.name, r, rules.max_rounds, dossier, list(reports), transcript,
                            list(memories.get(m.name, [])), _rules(rules)) for m in members]
        ballots = list(await asyncio.gather(*(_ballot(m, v) for m, v in zip(members, views))))
        outcome = tally(ballots, rules)
        rec = RoundRecord(r, ballots, outcome)
        rounds.append(rec)

        if outcome.kind in FINAL_OUTCOMES:
            return DebateResult(outcome.kind, rounds, outcome)
        if outcome.kind == "request_info" and r < rules.max_rounds:
            for team in outcome.teams:
                report = await teams.report(team, outcome.requests, dossier)
                entry = {"round": r, "team": team, "requests": outcome.requests,
                         "report": report}
                rec.reports.append(entry)
                reports.append(entry)

    return DebateResult("no_consensus", rounds, None)
