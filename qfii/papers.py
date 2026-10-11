"""Paper lifecycle. One paper = one stock analysis moving through the department.

Every state change goes through transition(), which allows only the moves in
TRANSITIONS and writes an event row, so the event log is the paper's history.

    candidate ──research──▶ packet_ready ──assessment──▶ proposal ──committee debate──┐
        │                       │  ▲                     ▲   (Risk & Validation runs   │
        ▼                       ▼  │ research            │    only if the committee   │
     dropped        rejected / deferred / needs_evidence │    votes to commission it) │
                                                         └──── sent_back ◀────────────┤
             decided ──execution──▶ executed ──sold──▶ closed     passed / shelved ◀──┘
                └──▶ exec_rejected (hard limits)
"""

from __future__ import annotations

import sqlite3

from .db import utcnow

TRANSITIONS: dict[str, set[str]] = {
    "candidate": {"packet_ready", "dropped"},
    "packet_ready": {"proposal", "rejected", "deferred", "needs_evidence", "dropped"},
    "needs_evidence": {"packet_ready", "dropped"},
    "deferred": {"packet_ready", "dropped"},
    "proposal": {"decided", "passed", "sent_back", "shelved"},
    "sent_back": {"proposal", "shelved"},
    "decided": {"executed", "exec_rejected"},
    "executed": {"closed"},
    # terminal
    "dropped": set(), "rejected": set(), "passed": set(), "shelved": set(),
    "exec_rejected": set(), "closed": set(),
}
STATES = frozenset(TRANSITIONS)
TERMINAL = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)
ACTIVE = STATES - TERMINAL


class IllegalTransition(ValueError):
    pass


def create_paper(conn: sqlite3.Connection, symbol: str, origin: str, actor: str,
                 note: str | None = None) -> int:
    now = utcnow()
    paper_id = conn.execute(
        "INSERT INTO papers (symbol, state, origin, created_at, updated_at) "
        "VALUES (?, 'candidate', ?, ?, ?)", (symbol, origin, now, now)).lastrowid
    conn.execute("INSERT INTO events (paper_id, from_state, to_state, actor, note, created_at) "
                 "VALUES (?, NULL, 'candidate', ?, ?, ?)", (paper_id, actor, note, now))
    return paper_id


def state_of(conn: sqlite3.Connection, paper_id: int) -> str:
    row = conn.execute("SELECT state FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if row is None:
        raise KeyError(f"no paper {paper_id}")
    return row[0]


def transition(conn: sqlite3.Connection, paper_id: int, to: str, actor: str,
               note: str | None = None) -> str:
    """Move a paper to `to`. Returns the previous state. Raises IllegalTransition."""
    if to not in STATES:
        raise IllegalTransition(f"unknown state {to!r}")
    current = state_of(conn, paper_id)
    if to not in TRANSITIONS[current]:
        raise IllegalTransition(f"paper {paper_id}: {current} -> {to} is not allowed "
                                f"(allowed: {sorted(TRANSITIONS[current]) or 'none, terminal'})")
    now = utcnow()
    # compare-and-set guards against two workers moving the same paper at once
    cur = conn.execute("UPDATE papers SET state = ?, updated_at = ? WHERE id = ? AND state = ?",
                       (to, now, paper_id, current))
    if cur.rowcount != 1:
        raise IllegalTransition(f"paper {paper_id} changed state concurrently")
    conn.execute("INSERT INTO events (paper_id, from_state, to_state, actor, note, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (paper_id, current, to, actor, note, now))
    return current


def history(conn: sqlite3.Connection, paper_id: int) -> list[tuple[str | None, str, str]]:
    return [tuple(r) for r in conn.execute(
        "SELECT from_state, to_state, actor FROM events WHERE paper_id = ? ORDER BY id",
        (paper_id,))]


def count_by_state(conn: sqlite3.Connection) -> dict[str, int]:
    return dict(conn.execute("SELECT state, COUNT(*) FROM papers GROUP BY state").fetchall())
