from dataclasses import replace
from datetime import date

import pytest

from qfii.agents.research.agent import ResearchAgent
from qfii.config import load_config
from qfii.db import connect, init_db
from qfii.discovery import (
    discovery_order,
    enqueue_request,
    queue_depth,
    run_cycle,
    run_loop,
    spent_today_usd,
)
from qfii.llm import FakeLLM
from qfii.papers import count_by_state, create_paper, history, transition
from qfii.store import record_llm_calls

AS_OF = date(2026, 10, 9)


@pytest.fixture
def setup(cfg):
    cfg = replace(cfg, symbols=("AAPL", "MSFT", "NVDA", "JPM", "XOM"), batch_size=2)
    conn = connect(cfg.db_path)
    init_db(conn, cfg.limits.starting_cash_usd)
    return cfg, conn


async def test_cycle_researches_a_batch_then_dedups(setup):
    cfg, conn = setup
    r1 = await run_cycle(cfg, conn, AS_OF)
    assert r1.researched == ["AAPL", "JPM"]  # alphabetical among never-researched
    r2 = await run_cycle(cfg, conn, AS_OF)
    assert r2.researched == ["MSFT", "NVDA"]
    assert r2.skipped == {}
    r3 = await run_cycle(cfg, conn, AS_OF)
    assert r3.researched == ["XOM"]
    assert r3.skipped == {s: "already in the pipeline" for s in ("AAPL", "JPM", "MSFT", "NVDA")}
    assert queue_depth(conn) == 5


async def test_recently_researched_symbol_is_skipped_even_if_finished(setup):
    cfg, conn = setup
    cfg = replace(cfg, symbols=("AAPL",))
    await run_cycle(cfg, conn, AS_OF)
    conn.execute("UPDATE papers SET state = 'rejected'")  # paper finished
    report = await run_cycle(cfg, conn, AS_OF)
    assert report.skipped == {"AAPL": "researched within 24h"}


async def test_queue_cap_stops_discovery(setup):
    cfg, conn = setup
    cfg = replace(cfg, queue_cap=1)
    report = await run_cycle(cfg, conn, AS_OF)
    assert report.researched == ["AAPL"] and "queue full" in report.stopped


async def test_budget_stops_discovery(setup):
    cfg, conn = setup
    record_llm_calls(conn, "research", "mid", [{"ok": True, "error": None, "provider": "x",
                                                 "model": "m", "prompt_tokens": 1,
                                                 "completion_tokens": 1, "cost_usd": 2.0}])
    assert spent_today_usd(conn, "research") == 2.0
    report = await run_cycle(cfg, conn, AS_OF)
    assert report.researched == [] and report.stopped == "research daily budget spent"


async def test_priced_model_accumulates_spend(setup):
    cfg, conn = setup
    llm_cfg = replace(cfg, batch_size=1)
    agent = ResearchAgent(llm_cfg)
    agent.llm.usd_per_m_input = agent.llm.usd_per_m_output = 1_000_000.0  # $1 per token
    report = await run_cycle(llm_cfg, conn, AS_OF, agent)
    assert report.researched == ["AAPL"]
    assert spent_today_usd(conn, "research") > cfg.agents["research"].daily_budget_usd
    assert (await run_cycle(llm_cfg, conn, AS_OF, agent)).stopped == "research daily budget spent"


async def test_screen_drops_illiquid_and_bad_data(setup, tmp_path):
    cfg, conn = setup
    cfg.csv_dir.mkdir(parents=True)
    rows = "\n".join(f"2026-{m:02d}-{d:02d},10,100" for m in range(1, 10) for d in range(1, 29))
    (cfg.csv_dir / "TINY.csv").write_text("date,close,volume\n" + rows + "\n2026-10-09,10,100\n")
    (cfg.csv_dir / "OLD.csv").write_text("date,close,volume\n2020-01-02,10,1000000\n")
    cfg = replace(cfg, data_source="csv", symbols=("OLD", "TINY"))
    report = await run_cycle(cfg, conn, AS_OF)
    assert report.dropped["OLD"].startswith("data quality: short_sample, stale_data")
    assert report.dropped["TINY"].startswith("illiquid")
    assert count_by_state(conn) == {"dropped": 2}
    assert history(conn, 1)[-1][2] == "screen"


async def test_requests_served_first_and_feed_waiting_paper(setup):
    cfg, conn = setup
    with conn:
        pid = create_paper(conn, "XOM", "discovery", "research")
        transition(conn, pid, "packet_ready", "research")
        transition(conn, pid, "needs_evidence", "assessment")
    enqueue_request(conn, "XOM", "refining margins?", "assessment", pid)
    enqueue_request(conn, "MSFT", "cloud growth?")
    report = await run_cycle(replace(cfg, batch_size=1), conn, AS_OF)
    assert report.served_requests == ["XOM", "MSFT"]
    assert [h[1] for h in history(conn, pid)][-1] == "packet_ready"  # same paper, resumed
    assert conn.execute("SELECT COUNT(*) FROM research_requests WHERE status='done'"
                        ).fetchone()[0] == 2
    assert report.researched == ["AAPL"]


async def test_research_failure_drops_paper_and_logs_calls(setup):
    cfg, conn = setup
    agent = ResearchAgent(cfg, FakeLLM(lambda s, u: "garbage"))
    report = await run_cycle(replace(cfg, symbols=("AAPL",)), conn, AS_OF, agent)
    assert "no valid notes" in report.dropped["AAPL"]
    assert count_by_state(conn) == {"dropped": 1}
    assert tuple(conn.execute("SELECT COUNT(*), SUM(ok) FROM llm_calls").fetchone()) == (2, 0)


def test_discovery_order_oldest_first(setup):
    _cfg, conn = setup
    conn.execute("INSERT INTO papers (symbol, state, created_at, updated_at) "
                 "VALUES ('AAPL', 'dropped', '2026-01-01', '2026-01-01'), "
                 "('MSFT', 'dropped', '2025-01-01', '2025-01-01')")
    assert discovery_order(conn, ("AAPL", "MSFT", "NVDA")) == ["NVDA", "MSFT", "AAPL"]


async def test_run_loop_counts_cycles(setup):
    cfg, conn = setup
    seen = []
    n = await run_loop(cfg, conn, 0, 2, AS_OF, lambda i, r: seen.append((i, r.researched)))
    assert n == 2 and seen == [(1, ["AAPL", "JPM"]), (2, ["MSFT", "NVDA"])]


def test_config_rejects_bad_batch(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[research]\nbatch_size = 0\n")
    with pytest.raises(ValueError):
        load_config(p)
