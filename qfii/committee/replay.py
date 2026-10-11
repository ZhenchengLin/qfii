"""What-if replay: re-count a finished debate's ballots under different rules.

Vote counting is deterministic, so for every round we can ask what the outcome
would have been under, say, pass_rule = "majority" or an early stalemate stop.
Members only react to earlier outcomes, so the replay is exact up to the first
round whose outcome differs: if that outcome ends the debate (a decision, a
send-back or the stalemate stop), the debate would have ended there with those
ballots. If it differs without ending the debate (e.g. a question answered
under one rule but not the other), the members would have seen a different
history; the replay stops and is marked inexact.

No LLM call is made, so rule changes can be measured on real debates for free.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..config import Committee
from ..contracts import Ballot
from .engine import FINAL_OUTCOMES, _unchanged_streak
from .protocol import tally


@dataclass
class Replay:
    outcome: str            # invest | replace | pass | send_back | no_consensus
    rounds: int             # rounds the debate would have taken
    exact: bool             # False: diverged without ending; rounds/outcome are actuals
    size_usd: float | None = None
    amendment_id: str | None = None


def _entry(ballots: list[Ballot]) -> dict[str, Any]:
    return {"ballots": [b.model_dump(exclude_none=True) for b in ballots]}


def replay_debate(rounds: list[list[Ballot]], actual: list[str], rules: Committee) -> Replay:
    """rounds: each round's ballots (propose ballots carry their amendment_id);
    actual: each round's real outcome kind under the rules the debate ran with."""
    amendments: dict[str, dict[str, Any]] = {}
    seen: list[dict[str, Any]] = []
    for i, ballots in enumerate(rounds[:rules.max_rounds], start=1):
        for b in ballots:
            if b.action == "propose" and b.amendment is not None and b.amendment_id:
                amendments[b.amendment_id] = b.amendment.model_dump(exclude_none=True)
        out = tally(ballots, rules, amendments)
        seen.append(_entry(ballots))
        if out.kind in FINAL_OUTCOMES:
            return Replay(out.kind, i, True, out.size_usd, out.amendment_id)
        if out.kind != actual[i - 1]:
            return Replay(_final_kind(actual), len(rounds), False)
        stop = rules.stop_after_unchanged_rounds
        if stop and out.kind == "continue" and _unchanged_streak(seen) > stop:
            return Replay("no_consensus", i, True)
    if len(rounds) > rules.max_rounds:
        return Replay("no_consensus", rules.max_rounds, True)
    return Replay(_final_kind(actual), len(rounds), True)


def _final_kind(actual: list[str]) -> str:
    return actual[-1] if actual and actual[-1] in FINAL_OUTCOMES else "no_consensus"


def replay_paper(debates: list[tuple[list[list[Ballot]], list[str]]],
                 rules: Committee) -> Replay:
    """A paper's debates in order (send-backs start a new debate). The paper ends at
    the first debate whose replay ends in anything but a send-back."""
    used, exact = 0, True
    for rounds, actual in debates:
        rep = replay_debate(rounds, actual, rules)
        used += rep.rounds
        exact = exact and rep.exact
        if rep.outcome != "send_back":
            return Replay(rep.outcome, used, exact, rep.size_usd, rep.amendment_id)
    return Replay("send_back", used, exact)


def from_records(records: list[Any]) -> tuple[list[list[Ballot]], list[str]]:
    """From the engine's RoundRecords (a DebateResult's rounds)."""
    return [list(r.ballots) for r in records], [r.outcome.kind for r in records]


def load_debate(conn: sqlite3.Connection, debate_id: int, rules: Committee
                ) -> tuple[list[list[Ballot]], list[str]]:
    """A saved debate's ballots. Round outcomes are not stored; they are recomputed
    with `rules`, which must be the rules the debate ran with."""
    by_round: dict[int, list[Ballot]] = {}
    for rnd, member, action, size, repl, target, request, argument, aid, ajson in conn.execute(
            "SELECT round, member, action, size_usd, replace_symbol, target, request, argument, "
            "amendment_id, amendment_json FROM ballots WHERE debate_id = ? ORDER BY round, id",
            (debate_id,)):
        raw = {"member": member, "action": action, "argument": argument, "size_usd": size,
               "replace_symbol": repl, "target": target, "request": request,
               "amendment_id": aid, "amendment": json.loads(ajson) if ajson else None}
        ballot = Ballot(**{k: v for k, v in raw.items() if v is not None})
        ballot.amendment_id = aid           # engine-assigned on propose
        by_round.setdefault(rnd, []).append(ballot)
    rounds = [by_round[r] for r in sorted(by_round)]
    amendments: dict[str, dict[str, Any]] = {}
    actual = []
    for ballots in rounds:
        for b in ballots:
            if b.action == "propose" and b.amendment is not None and b.amendment_id:
                amendments[b.amendment_id] = b.amendment.model_dump(exclude_none=True)
        actual.append(tally(ballots, rules, amendments).kind)
    return rounds, actual


def load_paper(conn: sqlite3.Connection, paper_id: int, rules: Committee
               ) -> list[tuple[list[list[Ballot]], list[str]]]:
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM debates WHERE paper_id = ? ORDER BY id", (paper_id,))]
    return [load_debate(conn, i, rules) for i in ids]

