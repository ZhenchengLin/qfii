"""Private memory per committee member. recall() only ever returns the asking
member's own rows; that isolation is what lets the three diverge over time."""

from __future__ import annotations

import sqlite3

from ..config import COMMITTEE
from ..db import utcnow


def remember(conn: sqlite3.Connection, member: str, kind: str, text: str,
             paper_id: int | None = None) -> None:
    if member not in COMMITTEE:
        raise ValueError(f"unknown committee member {member!r}")
    conn.execute("INSERT INTO member_memory (member, paper_id, kind, text, created_at) "
                 "VALUES (?, ?, ?, ?, ?)", (member, paper_id, kind, text, utcnow()))


def recall(conn: sqlite3.Connection, member: str, limit: int) -> list[str]:
    if limit <= 0:
        return []
    rows = conn.execute("SELECT kind, text FROM member_memory WHERE member = ? "
                        "ORDER BY id DESC LIMIT ?", (member, limit)).fetchall()
    return [f"[{kind}] {text}" for kind, text in reversed(rows)]


def recall_all(conn: sqlite3.Connection, limit: int) -> dict[str, list[str]]:
    return {m: recall(conn, m, limit) for m in COMMITTEE}
