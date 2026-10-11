"""SQLite connection and schema initialisation."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 5

# Upgrades for databases created by an older version:
#   {target version: (table the step rewrites, or None; SQL)} or a list of such steps.
# A step that rewrites a table is skipped if that table does not exist.
# schema.sql always holds the full current schema for new databases.
MIGRATIONS = {
    2: (None, """
CREATE TABLE IF NOT EXISTS investor_profile (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    capital_usd    REAL NOT NULL CHECK (capital_usd > 0),
    income_usd     REAL CHECK (income_usd IS NULL OR income_usd >= 0),
    filing_status  TEXT NOT NULL CHECK (filing_status IN ('single', 'married_joint')),
    state_tax_pct  REAL NOT NULL DEFAULT 0 CHECK (state_tax_pct BETWEEN 0 AND 20),
    updated_at     TEXT NOT NULL
);
"""),
    3: ("ballots", """
ALTER TABLE ballots RENAME TO ballots_v2;
CREATE TABLE ballots (
    id              INTEGER PRIMARY KEY,
    debate_id       INTEGER NOT NULL REFERENCES debates(id),
    round           INTEGER NOT NULL CHECK (round BETWEEN 1 AND 10),
    member          TEXT NOT NULL CHECK (member IN ('aggressive', 'conservative', 'neutral')),
    action          TEXT NOT NULL CHECK (action IN ('invest', 'replace', 'pass', 'request_info',
                                                    'send_back', 'abstain')),
    size_usd        REAL CHECK (size_usd IS NULL OR size_usd > 0),
    replace_symbol  TEXT,
    target          TEXT CHECK (target IN ('assessment', 'risk', 'both')),
    request         TEXT,
    argument        TEXT NOT NULL,
    llm_call_id     INTEGER REFERENCES llm_calls(id),
    created_at      TEXT NOT NULL,
    UNIQUE (debate_id, round, member),
    CHECK (action NOT IN ('invest', 'replace') OR size_usd IS NOT NULL),
    CHECK (action <> 'replace' OR replace_symbol IS NOT NULL),
    CHECK (action NOT IN ('request_info', 'send_back') OR (target IS NOT NULL AND request IS NOT NULL))
);
INSERT INTO ballots SELECT * FROM ballots_v2;
DROP TABLE ballots_v2;
"""),
    4: [("ballots", """
ALTER TABLE ballots RENAME TO ballots_v3;
CREATE TABLE ballots (
    id              INTEGER PRIMARY KEY,
    debate_id       INTEGER NOT NULL REFERENCES debates(id),
    round           INTEGER NOT NULL CHECK (round BETWEEN 1 AND 10),
    member          TEXT NOT NULL CHECK (member IN ('aggressive', 'conservative', 'neutral')),
    action          TEXT NOT NULL CHECK (action IN ('invest', 'replace', 'pass', 'request_info',
                                                    'send_back', 'propose', 'support',
                                                    'abstain')),
    size_usd        REAL CHECK (size_usd IS NULL OR size_usd > 0),
    replace_symbol  TEXT,
    target          TEXT CHECK (target IN ('assessment', 'risk', 'both')),
    request         TEXT,
    argument        TEXT NOT NULL,
    llm_call_id     INTEGER REFERENCES llm_calls(id),
    created_at      TEXT NOT NULL,
    amendment_id    TEXT,            -- propose: the id it received; support: the id backed
    amendment_json  TEXT,            -- propose: the proposed terms
    UNIQUE (debate_id, round, member),
    CHECK (action NOT IN ('invest', 'replace') OR size_usd IS NOT NULL),
    CHECK (action <> 'replace' OR replace_symbol IS NOT NULL),
    CHECK (action NOT IN ('request_info', 'send_back') OR (target IS NOT NULL AND request IS NOT NULL))
);
INSERT INTO ballots (id, debate_id, round, member, action, size_usd, replace_symbol, target, request, argument, llm_call_id, created_at) SELECT id, debate_id, round, member, action, size_usd, replace_symbol, target, request, argument, llm_call_id, created_at FROM ballots_v3;
DROP TABLE ballots_v3;
"""), ("decisions", """
ALTER TABLE decisions ADD COLUMN amendment_id TEXT;
ALTER TABLE decisions ADD COLUMN terms_json TEXT;
""")],
    5: ("ballots", "ALTER TABLE ballots ADD COLUMN changed_because TEXT;"),
}


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
    """Create the schema and seed starting cash, or upgrade an older database.
    Returns True only when a new database was created."""
    current = schema_version(conn)
    if current == SCHEMA_VERSION:
        return False
    if current > SCHEMA_VERSION:
        raise RuntimeError(f"database is schema v{current}, newer than this code "
                           f"(v{SCHEMA_VERSION})")
    if current:
        with conn:
            for version in range(current + 1, SCHEMA_VERSION + 1):
                steps = MIGRATIONS[version]
                for table, sql in steps if isinstance(steps, list) else [steps]:
                    if table is None or table in tables(conn):
                        conn.executescript(sql)
                conn.execute(f"PRAGMA user_version = {version}")
        return False
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
