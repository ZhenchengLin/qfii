"""SQLite connection and schema initialisation."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 1


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def init_db(conn: sqlite3.Connection, starting_cash_usd: float) -> bool:
    """Create the schema and seed starting cash. Returns False if already initialised."""
    current = schema_version(conn)
    if current == SCHEMA_VERSION:
        return False
    if current != 0:
        raise RuntimeError(f"database is schema v{current}, expected v{SCHEMA_VERSION}")
    sql = resources.files("qfii").joinpath("schema.sql").read_text()
    with conn:
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO cash_ledger (amount_cents, reason, created_at) VALUES (?, 'deposit', ?)",
            (round(starting_cash_usd * 100), utcnow()),
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return True


def cash_balance_cents(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM cash_ledger").fetchone()[0]


def tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]
