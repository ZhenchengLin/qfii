import sqlite3

import pytest

from qfii.db import SCHEMA_VERSION, cash_balance_cents, connect, init_db, tables, utcnow

EXPECTED = {
    "debates", "ballots", "info_requests", "member_memory", "lots", "papers", "events",
    "evidence", "research_requests", "packets", "proposals", "reviews",
    "decisions", "orders", "fills", "cash_ledger", "llm_calls",
}


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c, 100_000.0)
    return c


def test_init_creates_tables_and_cash(conn):
    assert EXPECTED <= set(tables(conn))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert cash_balance_cents(conn) == 10_000_000


def test_init_is_idempotent(conn):
    assert init_db(conn, 100_000.0) is False
    assert cash_balance_cents(conn) == 10_000_000


def test_file_db(tmp_path):
    path = tmp_path / "sub" / "a.db"
    assert init_db(connect(path), 500.0) is True
    assert init_db(connect(path), 500.0) is False
    assert cash_balance_cents(connect(path)) == 50_000


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO events (paper_id, to_state, actor, created_at) "
                     "VALUES (999, 'candidate', 'research', ?)", (utcnow(),))


def test_limit_order_needs_price(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO orders (symbol, side, qty, order_type, status, created_at) "
                     "VALUES ('AAPL', 'buy', 1, 'limit', 'submitted', ?)", (utcnow(),))



def _debate(conn):
    now = utcnow()
    pid = conn.execute("INSERT INTO papers (symbol, created_at, updated_at) VALUES ('AAPL', ?, ?)",
                       (now, now)).lastrowid
    return conn.execute("INSERT INTO debates (paper_id, started_at) VALUES (?, ?)",
                        (pid, now)).lastrowid


BALLOT = ("INSERT INTO ballots (debate_id, round, member, action, size_usd, replace_symbol, "
          "target, request, argument, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'a', ?)")


def test_one_ballot_per_member_per_round(conn):
    d, now = _debate(conn), utcnow()
    conn.execute(BALLOT, (d, 1, "neutral", "invest", 5000, None, None, None, now))
    conn.execute(BALLOT, (d, 2, "neutral", "pass", None, None, None, None, now))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(BALLOT, (d, 1, "neutral", "pass", None, None, None, None, now))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(BALLOT, (d, 3, "chair", "pass", None, None, None, None, now))


@pytest.mark.parametrize("row", [
    ("aggressive", "invest", None, None, None, None),         # invest without size
    ("aggressive", "replace", 100, None, None, None),         # replace without symbol
    ("neutral", "request_info", None, None, None, "why?"),    # no target
    ("neutral", "send_back", None, None, "assessment", None), # no feedback
])
def test_ballot_fields_must_match_action(conn, row):
    d, now = _debate(conn), utcnow()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(BALLOT, (d, 1, *row, now))


def test_lot_open_qty_cannot_exceed_qty(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO lots (symbol, qty, qty_open, cost_per_share, opened_at) "
                     "VALUES ('AAPL', 1, 2, 10, ?)", (utcnow(),))
