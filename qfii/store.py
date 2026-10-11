"""Persist agent output to SQLite."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .contracts import EvidencePacket, ResearchRequest
from .db import utcnow

_ORIGIN = {"discovery": "discovery", "user": "user"}


def record_llm_calls(conn: sqlite3.Connection, agent: str, tier: str,
                     calls: list[dict[str, Any]], paper_id: int | None = None) -> list[int]:
    ids = []
    for c in calls:
        cur = conn.execute(
            "INSERT INTO llm_calls (agent, tier, provider, model, paper_id, prompt_tokens, "
            "completion_tokens, cost_usd, ok, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (agent, tier, c["provider"], c["model"], paper_id, c["prompt_tokens"],
             c["completion_tokens"], int(c["ok"]), c["error"], utcnow()),
        )
        ids.append(cur.lastrowid)
    return ids


def save_packet(conn: sqlite3.Connection, request: ResearchRequest, packet: EvidencePacket,
                llm_calls: list[dict[str, Any]], tier: str) -> int:
    """Create a paper in state packet_ready with its evidence and packet. Returns paper id."""
    now = utcnow()
    with conn:
        paper_id = conn.execute(
            "INSERT INTO papers (symbol, state, origin, created_at, updated_at) "
            "VALUES (?, 'packet_ready', ?, ?, ?)",
            (packet.symbol, _ORIGIN.get(request.requested_by, "request"), now, now),
        ).lastrowid
        conn.executemany(
            "INSERT INTO events (paper_id, from_state, to_state, actor, note, created_at) "
            "VALUES (?, ?, ?, 'research', ?, ?)",
            [(paper_id, None, "candidate", request.question, now),
             (paper_id, "candidate", "packet_ready", None, now)],
        )
        for ev in packet.evidence:
            conn.execute(
                "INSERT OR IGNORE INTO evidence (symbol, kind, source, as_of, retrieved_at, "
                "evidence_key, payload_json, params_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ev.symbol, ev.kind, ev.source, ev.as_of, ev.retrieved_at, ev.id,
                 json.dumps(ev.payload), json.dumps(ev.params)),
            )
        call_ids = record_llm_calls(conn, "research", tier, llm_calls, paper_id)
        ok_ids = [i for i, c in zip(call_ids, llm_calls) if c["ok"]]
        body = packet.model_dump(mode="json", exclude={"evidence"})
        conn.execute(
            "INSERT INTO packets (paper_id, body_json, evidence_ids, quality_flags, llm_call_id, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (paper_id, json.dumps(body), json.dumps([e.id for e in packet.evidence]),
             json.dumps(packet.quality_flags), ok_ids[-1] if ok_ids else None, now),
        )
    return paper_id


# paper state after a committee debate
DEBATE_STATE = {"invest": "decided", "replace": "decided", "pass": "passed",
                "send_back": "sent_back", "no_consensus": "shelved"}


def save_debate(conn: sqlite3.Connection, paper_id: int, symbol: str, result,
                member_calls: dict[str, tuple[str, list[dict[str, Any]]]] | None = None) -> int:
    """Store a finished committee debate (qfii.committee.engine.DebateResult).
    member_calls maps member -> (model tier, LLM call records)."""
    from .committee.memory import remember

    now = utcnow()
    with conn:
        debate_id = conn.execute(
            "INSERT INTO debates (paper_id, outcome, rounds_used, started_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (paper_id, result.outcome, len(result.rounds), now, now),
        ).lastrowid
        for rec in result.rounds:
            for b in rec.ballots:
                conn.execute(
                    "INSERT INTO ballots (debate_id, round, member, action, size_usd, "
                    "replace_symbol, target, request, argument, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (debate_id, rec.round, b.member, b.action, b.size_usd, b.replace_symbol,
                     b.target, b.request, b.argument, now),
                )
            for rep in rec.reports:
                conn.execute(
                    "INSERT INTO info_requests (debate_id, round, team, questions, report_json, "
                    "created_at, answered_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (debate_id, rec.round, rep["team"], json.dumps(rep["requests"]),
                     json.dumps(rep["report"]), now, now),
                )
        if result.outcome in ("invest", "replace", "pass"):
            conn.execute(
                "INSERT INTO decisions (paper_id, debate_id, decision, size_usd, replace_symbol, "
                "rounds, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (paper_id, debate_id, result.outcome, result.final.size_usd,
                 result.final.replace_symbol, len(result.rounds), now),
            )
        old = conn.execute("SELECT state FROM papers WHERE id = ?", (paper_id,)).fetchone()[0]
        new = DEBATE_STATE[result.outcome]
        conn.execute("UPDATE papers SET state = ?, updated_at = ? WHERE id = ?",
                     (new, now, paper_id))
        conn.execute("INSERT INTO events (paper_id, from_state, to_state, actor, note, created_at) "
                     "VALUES (?, ?, ?, 'committee', ?, ?)",
                     (paper_id, old, new, (f"debate #{debate_id}: {result.outcome} after "
                                          f"{len(result.rounds)} round(s)"), now))
        # each member remembers its own last stance, plus any note it chose to keep
        last = {b.member: b for rec in result.rounds for b in rec.ballots}
        for member, ballot in last.items():
            remember(conn, member, "reflection",
                     f"{symbol}: committee outcome {result.outcome} after {len(result.rounds)} "
                     f"round(s); my last vote was {ballot.action}"
                     + (f" ${ballot.size_usd:,.0f}" if ballot.size_usd else ""), paper_id)
            notes = [b.note_to_self for rec in result.rounds for b in rec.ballots
                     if b.member == member and b.note_to_self]
            for note in notes:
                remember(conn, member, "lesson", note, paper_id)
        for member, (tier, calls) in (member_calls or {}).items():
            record_llm_calls(conn, member, tier, calls, paper_id)
    return debate_id
