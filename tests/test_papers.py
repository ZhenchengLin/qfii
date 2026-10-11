import sqlite3

import pytest

from qfii.db import connect, init_db
from qfii.papers import (
    ACTIVE,
    STATES,
    TERMINAL,
    TRANSITIONS,
    IllegalTransition,
    count_by_state,
    create_paper,
    history,
    state_of,
    transition,
)


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c, 1000)
    return c


def test_happy_path_to_closed(conn):
    pid = create_paper(conn, "AAPL", "discovery", "research")
    for to, actor in [("packet_ready", "research"), ("proposal", "assessment"),
                      ("risk_reviewed", "risk"), ("decided", "committee"),
                      ("executed", "execution"), ("closed", "execution")]:
        transition(conn, pid, to, actor)
    assert state_of(conn, pid) == "closed"
    assert [h[1] for h in history(conn, pid)] == ["candidate", "packet_ready", "proposal",
                                                  "risk_reviewed", "decided", "executed",
                                                  "closed"]


def test_send_back_loop(conn):
    pid = create_paper(conn, "AAPL", "user", "research")
    for to in ["packet_ready", "proposal", "risk_reviewed", "sent_back", "proposal",
               "risk_reviewed", "shelved"]:
        transition(conn, pid, to, "x")
    assert state_of(conn, pid) == "shelved"


@pytest.mark.parametrize("path, bad", [
    ([], "decided"),                              # cannot skip the whole pipeline
    (["packet_ready"], "decided"),                # cannot skip reports and debate
    (["packet_ready", "proposal"], "decided"),    # cannot skip Risk
    (["dropped"], "packet_ready"),                # terminal stays terminal
    ([], "teleported"),                           # unknown state
])
def test_illegal_transitions(conn, path, bad):
    pid = create_paper(conn, "AAPL", "user", "research")
    for to in path:
        transition(conn, pid, to, "x")
    with pytest.raises(IllegalTransition):
        transition(conn, pid, bad, "x")


def test_every_target_is_a_known_state():
    assert all(t in STATES for nxt in TRANSITIONS.values() for t in nxt)
    assert TERMINAL == {"dropped", "rejected", "passed", "shelved", "exec_rejected", "closed"}
    assert "risk_reviewed" in ACTIVE


def test_db_rejects_unknown_state(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO papers (symbol, state, created_at, updated_at) "
                     "VALUES ('X', 'teleported', 'now', 'now')")


def test_count_by_state(conn):
    a = create_paper(conn, "A", "user", "r")
    create_paper(conn, "B", "user", "r")
    transition(conn, a, "dropped", "r")
    assert count_by_state(conn) == {"candidate": 1, "dropped": 1}
