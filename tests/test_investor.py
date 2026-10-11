import sqlite3
from datetime import date

import pytest

from qfii.db import SCHEMA_VERSION, cash_balance_cents, connect, init_db, schema_version, tables
from qfii.investor import SetupError, get_profile, set_profile
from qfii.portfolio import snapshot
from qfii.tax import tax_profile


@pytest.mark.parametrize("income, filing, ordinary, ltcg, niit", [
    (16_000, "single", 10, 0, 0),           # below the standard deduction
    (60_000, "single", 12, 0, 0),           # taxable 43,900
    (90_000, "single", 22, 15, 0),          # taxable 73,900
    (150_000, "single", 24, 15, 0),
    (250_000, "single", 32, 15, 3.8),       # NIIT above $200k
    (900_000, "single", 37, 20, 3.8),
    (150_000, "married_joint", 22, 15, 0),  # taxable 117,800
    (300_000, "married_joint", 24, 15, 3.8),
])
def test_brackets(income, filing, ordinary, ltcg, niit):
    t = tax_profile(income, filing)
    assert (t.ordinary_bracket_pct, t.ltcg_pct, t.niit_pct) == (ordinary, ltcg, niit)
    assert t.short_term_pct == ordinary + niit and t.long_term_pct == ltcg + niit
    assert not t.assumed


def test_missing_income_is_a_flagged_assumption():
    t = tax_profile(None, "single", state_pct=5)
    assert t.assumed and (t.short_term_pct, t.long_term_pct) == (29, 20)
    assert "assumed" in t.basis


def test_invalid_tax_inputs():
    with pytest.raises(ValueError):
        tax_profile(50_000, "head_of_household")
    with pytest.raises(ValueError):
        tax_profile(-1)


def test_setup_sets_cash_and_profile(cfg):
    conn = connect(":memory:")
    init_db(conn, 100_000)
    set_profile(conn, 25_000, 90_000, "single", 9.3)
    assert cash_balance_cents(conn) == 2_500_000
    p = get_profile(conn)
    assert (p.capital_usd, p.filing_status) == (25_000, "single")
    snap = snapshot(conn, cfg, date(2026, 10, 9))
    assert snap.cash_usd == 25_000 and snap.investor["capital_usd"] == 25_000
    assert snap.tax["short_term_pct"] == pytest.approx(31.3)  # 22 + 9.3 state
    assert snap.tax["long_term_after_days"] == 365
    # re-running setup before any trade replaces the capital
    set_profile(conn, 30_000)
    assert cash_balance_cents(conn) == 3_000_000


def test_capital_frozen_after_trading():
    conn = connect(":memory:")
    init_db(conn, 100_000)
    set_profile(conn, 25_000)
    conn.execute("INSERT INTO cash_ledger (amount_cents, reason, created_at) "
                 "VALUES (-100000, 'buy', 'now')")
    with pytest.raises(SetupError, match="capital can no longer"):
        set_profile(conn, 50_000)
    set_profile(conn, 25_000, income_usd=200_000)  # tax details can still change
    assert get_profile(conn).income_usd == 200_000


def test_snapshot_without_profile_uses_flagged_default(cfg):
    conn = connect(":memory:")
    init_db(conn, 100_000)
    snap = snapshot(conn, cfg, date(2026, 10, 9))
    assert snap.tax["assumed"] is True and snap.investor["profile_set"] is False


def test_v1_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cash_ledger (id INTEGER PRIMARY KEY, amount_cents INTEGER, "
                 "reason TEXT, ref_type TEXT, ref_id INTEGER, created_at TEXT)")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    conn = connect(path)
    assert init_db(conn, 1000) is False
    assert schema_version(conn) == SCHEMA_VERSION and "investor_profile" in tables(conn)


def test_v2_ballots_table_gets_abstain(tmp_path):
    from qfii.db import MIGRATIONS

    path = tmp_path / "v2.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ballots (id INTEGER PRIMARY KEY, debate_id INTEGER, round INTEGER, "
                 "member TEXT, action TEXT CHECK (action IN ('pass')), size_usd REAL, "
                 "replace_symbol TEXT, target TEXT, request TEXT, argument TEXT, "
                 "llm_call_id INTEGER, created_at TEXT)")
    conn.execute("INSERT INTO ballots VALUES (1, 1, 1, 'neutral', 'pass', NULL, NULL, NULL, NULL, "
                 "'a', NULL, 'now')")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()
    conn = connect(path)
    conn.execute("PRAGMA foreign_keys = OFF")  # bare test table has no debates to reference
    init_db(conn, 1000)
    conn.execute("INSERT INTO ballots (debate_id, round, member, action, argument, created_at) "
                 "VALUES (1, 2, 'neutral', 'abstain', 'x', 'now')")
    assert conn.execute("SELECT COUNT(*) FROM ballots").fetchone()[0] == 2
    assert 3 in MIGRATIONS


def test_v3_database_gets_amendment_columns(tmp_path):
    path = tmp_path / "v3.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ballots (id INTEGER PRIMARY KEY, debate_id INTEGER, round INTEGER, "
                 "member TEXT, action TEXT CHECK (action IN ('pass', 'abstain')), size_usd REAL, "
                 "replace_symbol TEXT, target TEXT, request TEXT, argument TEXT, "
                 "llm_call_id INTEGER, created_at TEXT)")
    conn.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY, paper_id INTEGER, "
                 "debate_id INTEGER, decision TEXT, size_usd REAL, replace_symbol TEXT, "
                 "rounds INTEGER, created_at TEXT)")
    conn.execute("INSERT INTO ballots VALUES (1, 1, 1, 'neutral', 'pass', NULL, NULL, NULL, NULL, "
                 "'a', NULL, 'now')")
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()
    conn = connect(path)
    conn.execute("PRAGMA foreign_keys = OFF")
    init_db(conn, 1000)
    conn.execute("INSERT INTO ballots (debate_id, round, member, action, argument, created_at, "
                 "amendment_id) VALUES (1, 2, 'neutral', 'support', 'x', 'now', 'A1')")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(decisions)")]
    assert "terms_json" in cols and conn.execute("SELECT COUNT(*) FROM ballots").fetchone()[0] == 2


def test_v6_database_gets_round_results_column(tmp_path):
    path = tmp_path / "v6.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE debates (id INTEGER PRIMARY KEY, paper_id INTEGER, "
                 "outcome TEXT, rounds_used INTEGER, started_at TEXT, ended_at TEXT)")
    conn.execute("INSERT INTO debates VALUES (1, 1, 'pass', 2, 'now', 'now')")
    conn.execute("PRAGMA user_version = 6")
    conn.commit()
    conn.close()
    conn = connect(path)
    init_db(conn, 1000)
    assert schema_version(conn) == SCHEMA_VERSION
    assert tuple(conn.execute("SELECT outcome, round_results_json FROM debates").fetchone()) \
        == ("pass", None)
