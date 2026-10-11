"""Continuous discovery: the undergrads never stop looking.

Each cycle:
  1. serve open research_requests first (targeted follow-ups), oldest first;
  2. if the Assessment queue (papers in packet_ready) is below research.queue_cap,
     scan the universe, least-recently-researched first, up to research.batch_size;
  3. before any LLM call: skip symbols with an active paper or researched within
     research.cache_ttl_hours (dedup), drop symbols failing the cheap deterministic
     screen (data quality, liquidity), and stop when the Research agent's daily
     budget is spent.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from .agents.research.agent import ResearchAgent, ResearchFailed
from .config import Config
from .contracts import ResearchRequest
from .db import utcnow
from .papers import ACTIVE, create_paper, transition
from .store import record_llm_calls, save_packet
from .tools.client import ToolBox

BLOCKING_FLAGS = {"stale_data", "short_sample", "missing_values"}


@dataclass
class CycleReport:
    served_requests: list[str] = field(default_factory=list)
    researched: list[str] = field(default_factory=list)
    dropped: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    stopped: str | None = None

    def summary(self) -> str:
        parts = [f"requests served: {', '.join(self.served_requests) or '-'}",
                 f"researched: {', '.join(self.researched) or '-'}"]
        if self.dropped:
            parts.append("dropped: " + ", ".join(f"{s} ({r})" for s, r in self.dropped.items()))
        if self.skipped:
            parts.append("skipped: " + ", ".join(f"{s} ({r})" for s, r in self.skipped.items()))
        if self.stopped:
            parts.append(f"stopped: {self.stopped}")
        return "; ".join(parts)


# ---------------------------------------------------------------- bookkeeping

def spent_today_usd(conn: sqlite3.Connection, agent: str) -> float:
    today = datetime.now(UTC).date().isoformat()
    return conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls "
                        "WHERE agent = ? AND substr(created_at, 1, 10) = ?",
                        (agent, today)).fetchone()[0]


def over_budget(conn: sqlite3.Connection, cfg: Config, agent: str) -> bool:
    return spent_today_usd(conn, agent) >= cfg.agents[agent].daily_budget_usd


def queue_depth(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM papers WHERE state = 'packet_ready'").fetchone()[0]


def _has_active_paper(conn: sqlite3.Connection, symbol: str) -> bool:
    marks = ",".join("?" * len(ACTIVE))
    return conn.execute(f"SELECT 1 FROM papers WHERE symbol = ? AND state IN ({marks}) LIMIT 1",
                        (symbol, *sorted(ACTIVE))).fetchone() is not None


def _researched_recently(conn: sqlite3.Connection, symbol: str, ttl_hours: int) -> bool:
    since = (datetime.now(UTC) - timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
    return conn.execute("SELECT 1 FROM papers WHERE symbol = ? AND created_at >= ? LIMIT 1",
                        (symbol, since)).fetchone() is not None


def discovery_order(conn: sqlite3.Connection, symbols: tuple[str, ...]) -> list[str]:
    """Never-researched symbols first, then least recently researched."""
    last = dict(conn.execute("SELECT symbol, MAX(created_at) FROM papers GROUP BY symbol"))
    return sorted(symbols, key=lambda s: (s in last, last.get(s, ""), s))


def enqueue_request(conn: sqlite3.Connection, symbol: str, question: str,
                    requested_by: str = "user", paper_id: int | None = None) -> int:
    with conn:
        return conn.execute(
            "INSERT INTO research_requests (paper_id, symbol, requested_by, question, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (paper_id, symbol.upper(), requested_by, question, utcnow())).lastrowid


# ---------------------------------------------------------------- screening

async def screen(cfg: Config, symbol: str, as_of: date) -> str | None:
    """Cheap deterministic screen, no LLM. Returns a drop reason or None."""
    async with ToolBox(cfg) as tools:
        quality = await tools.call("check_data_quality", symbol=symbol, as_of=as_of.isoformat())
        blocking = sorted(BLOCKING_FLAGS & set(quality["flags"]))
        if blocking:
            return "data quality: " + ", ".join(blocking)
        hist = await tools.call("get_price_history", symbol=symbol, as_of=as_of.isoformat(),
                                last_n=20)
    bars = hist["bars"]
    adv = sum(b["close"] * b["volume"] for b in bars) / len(bars)
    if adv < cfg.min_avg_daily_volume_usd:
        return f"illiquid: avg daily volume ${adv:,.0f} < ${cfg.min_avg_daily_volume_usd:,.0f}"
    return None


# ---------------------------------------------------------------- one cycle

async def _research(cfg: Config, conn: sqlite3.Connection, agent: ResearchAgent,
                    request: ResearchRequest, paper_id: int | None) -> tuple[bool, str]:
    tier = cfg.agents["research"].tier
    try:
        packet, calls = await agent.run(request)
    except ResearchFailed as exc:
        with conn:
            if paper_id is None:
                paper_id = create_paper(conn, request.symbol, _origin(request), "research",
                                        request.question)
            record_llm_calls(conn, "research", tier, exc.llm_calls, paper_id)
            transition(conn, paper_id, "dropped", "research", f"research failed: {exc}"[:500])
        return False, str(exc)
    save_packet(conn, request, packet, calls, tier, paper_id)
    return True, f"{len(packet.observations)} observations"


def _origin(request: ResearchRequest) -> str:
    return {"discovery": "discovery", "user": "user"}.get(request.requested_by, "request")


async def run_cycle(cfg: Config, conn: sqlite3.Connection, as_of: date | None = None,
                    agent: ResearchAgent | None = None) -> CycleReport:
    as_of = as_of or date.today()
    agent = agent or ResearchAgent(cfg)
    report = CycleReport()

    # 1. targeted requests
    requests = conn.execute("SELECT id, paper_id, symbol, requested_by, question "
                            "FROM research_requests WHERE status = 'open' ORDER BY id").fetchall()
    for req_id, paper_id, symbol, by, question in requests:
        if over_budget(conn, cfg, "research"):
            report.stopped = "research daily budget spent"
            return report
        # a request about a paper waiting for evidence feeds that paper; otherwise a new one
        if paper_id is not None and conn.execute(
                "SELECT state FROM papers WHERE id = ?", (paper_id,)).fetchone()[0] not in (
                "needs_evidence", "deferred"):
            paper_id = None
        request = ResearchRequest(symbol=symbol, as_of=as_of, question=question,
                                  requested_by=by)
        ok, detail = await _research(cfg, conn, agent, request, paper_id)
        with conn:
            conn.execute("UPDATE research_requests SET status = ?, done_at = ? WHERE id = ?",
                         ("done" if ok else "dropped", utcnow(), req_id))
        if ok:
            report.served_requests.append(symbol)
        else:
            report.dropped[symbol] = detail

    # 2. open discovery
    researched = 0
    for symbol in discovery_order(conn, cfg.symbols):
        if researched >= cfg.batch_size:
            break
        if queue_depth(conn) >= cfg.queue_cap:
            report.stopped = f"assessment queue full ({cfg.queue_cap})"
            break
        if over_budget(conn, cfg, "research"):
            report.stopped = "research daily budget spent"
            break
        if _has_active_paper(conn, symbol):
            report.skipped[symbol] = "already in the pipeline"
            continue
        if _researched_recently(conn, symbol, cfg.cache_ttl_hours):
            report.skipped[symbol] = f"researched within {cfg.cache_ttl_hours}h"
            continue
        reason = await screen(cfg, symbol, as_of)
        if reason:
            with conn:
                pid = create_paper(conn, symbol, "discovery", "screen")
                transition(conn, pid, "dropped", "screen", reason)
            report.dropped[symbol] = reason
            continue
        request = ResearchRequest(symbol=symbol, as_of=as_of, requested_by="discovery")
        ok, detail = await _research(cfg, conn, agent, request, None)
        if ok:
            report.researched.append(symbol)
            researched += 1
        else:
            report.dropped[symbol] = detail
    return report


async def run_loop(cfg: Config, conn: sqlite3.Connection, interval_s: float,
                   max_cycles: int | None = None, as_of: date | None = None,
                   on_cycle: Callable[[int, CycleReport], None] | None = None) -> int:
    """Run cycles until max_cycles (None = forever). Returns cycles run."""
    agent = ResearchAgent(cfg)
    n = 0
    while max_cycles is None or n < max_cycles:
        n += 1
        report = await run_cycle(cfg, conn, as_of, agent)
        if on_cycle:
            on_cycle(n, report)
        if max_cycles is None or n < max_cycles:
            await asyncio.sleep(interval_s)
    return n
