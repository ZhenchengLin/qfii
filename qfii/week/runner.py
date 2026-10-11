"""The week replay: the bottom-up department running on a simulated clock.

At every slot (10-minute mark of market time, Oct 5-9 by default):

  1. Research (per stock, code only first): check what changed since its last
     report -- a filing or 8-K the SEC accepted, a price move of
     week.price_move_pct, or a question from Assessment. Only then does the
     Research model rewrite the report (with the question searched in the
     filings). The numbers themselves refresh every slot for free.
  2. Assessment reads every report together (at the open, and whenever one
     changed) and decides
     per stock: advance / hold / dig (Research rewrites with its questions) /
     reject. It can calculate missing figures with the tools itself.
  3. Each advanced stock (at most once a day, or again on a new filing) gets a
     proposal written by Assessment and a committee debate in which Assessment
     sits as the neutral member, with Aggressive and Conservative, under the
     normal committee rules.

Everything is saved as it happens (papers, debates, week_events), the run can
resume after a crash, and it stops if a market day writes more than
week.gb_per_day.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..agents.research.agent import ResearchAgent, ResearchFailed
from ..agents.structured import AgentFailed
from ..agents.teams.agents import AssessmentAgent
from ..clock import AsOf, trading_slots
from ..committee.runner import debate_paper
from ..config import Config
from ..contracts import EvidenceItem, EvidencePacket, ResearchRequest
from ..db import utcnow
from ..papers import state_of, transition
from ..portfolio import snapshot
from ..store import record_llm_calls, save_packet, save_proposal
from ..tools.market import DataError, load_prices
from ..tools.sec import SecSource, visible

MAX_DIGS_PER_DAY = 2          # per stock: Assessment's "dig deeper" requests
RESEARCH_CONCURRENCY = 5


@dataclass
class Tracked:
    """What the department knows about one stock between slots."""
    symbol: str
    paper_id: int | None = None           # paper holding the current report
    report_at: str | None = None
    report_price: float | None = None
    filings: list[str] = field(default_factory=list)   # accessions seen by the report
    questions: list[str] = field(default_factory=list)  # Assessment's pending questions
    answering: str | None = None          # the question the current report answers
    digs: dict[str, int] = field(default_factory=dict)  # day -> dig requests
    debated_day: str | None = None
    debate_filings: list[str] = field(default_factory=list)
    last_decision: str | None = None
    last_reason: str | None = None


# ------------------------------------------------------------------ probes (no LLM)

def price_now(cfg: Config, symbol: str, cut: AsOf) -> float | None:
    try:
        bars = load_prices(cfg.data_source, cfg.csv_dir, symbol, cut, cfg.intraday_dir)
    except DataError:
        return None
    return bars[-1].close if bars else None


def visible_filings(sec: SecSource | None, symbol: str, cut: AsOf) -> list[dict[str, Any]]:
    """Filings public at the cutoff, from the dataset's offline manifest."""
    listed = sec.manifest(symbol) if sec else None
    return [{"accession": f.accession_no, "form": f.form,
             "accepted": f.accepted.strftime("%Y-%m-%d %H:%M") if f.accepted else None}
            for f in listed or [] if visible(f.filing_date, f.accepted, cut)]


def research_reason(t: Tracked, price: float | None, filings: list[dict[str, Any]],
                    move_pct: float) -> str | None:
    """Why the report must be rewritten now, or None to keep it."""
    if t.paper_id is None:
        return "first report"
    new = [f for f in filings if f["accession"] not in t.filings]
    if new:
        return "new filing: " + ", ".join(f"{f['form']} accepted {f['accepted']}" for f in new)
    if t.questions:
        return "Assessment asked: " + " | ".join(t.questions)
    if price and t.report_price and move_pct and \
            abs(price / t.report_price - 1) * 100 >= move_pct:
        return f"price moved {(price / t.report_price - 1) * 100:+.1f}% since the report"
    return None


# ------------------------------------------------------------------ persistence

def load_packet(conn: sqlite3.Connection, paper_id: int) -> EvidencePacket:
    body, ids = conn.execute("SELECT body_json, evidence_ids FROM packets WHERE paper_id = ? "
                             "ORDER BY id DESC LIMIT 1", (paper_id,)).fetchone()
    wanted = json.loads(ids)
    rows = {r[0]: r for r in conn.execute(
        f"SELECT evidence_key, symbol, kind, source, as_of, retrieved_at, payload_json, "
        f"params_json FROM evidence WHERE evidence_key IN ({','.join('?' * len(wanted))})",
        wanted)} if wanted else {}
    evidence = [EvidenceItem(id=k, symbol=r[1], kind=r[2], source=r[3], as_of=r[4],
                             retrieved_at=r[5], payload=json.loads(r[6]),
                             params=json.loads(r[7] or "null")) for k in wanted
                if (r := rows.get(k))]
    return EvidencePacket(**json.loads(body), evidence=evidence)


class Week:
    def __init__(self, cfg: Config, conn: sqlite3.Connection,
                 on_event: Callable[[dict[str, Any]], None] | None = None):
        self.cfg, self.conn = cfg, conn
        self.on_event = on_event
        manifest = json.loads((cfg.week.dataset_dir / "manifest.json").read_text())
        self.symbols: list[str] = manifest["symbols"]
        self.names: dict[str, str] = manifest.get("names", {})
        self.slots = trading_slots(cfg.week.start, cfg.week.end, cfg.week.cadence_minutes)
        self.tracked = {s: Tracked(s) for s in self.symbols}
        self.packets: dict[str, EvidencePacket] = {}
        self.assessment = AssessmentAgent(cfg)
        try:
            self.sec: SecSource | None = SecSource(cfg.sec_identity or "offline", cfg.sec_cache_dir,
                                                   lambda s: None)
        except Exception:  # noqa: BLE001 - no SEC data: research still runs on prices
            self.sec = None
        self.run_id: int | None = None
        self.stop_requested = False

    # -------------------------------------------------- run bookkeeping
    def event(self, slot: AsOf, kind: str, symbol: str | None = None, paper_id=None,
              debate_id=None, **detail: Any) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO week_events (run_id, slot, symbol, kind, detail_json, paper_id, "
                "debate_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (self.run_id, slot.iso(), symbol, kind, json.dumps(detail, default=str),
                 paper_id, debate_id, utcnow()))
        if self.on_event:
            self.on_event({"slot": slot.iso(), "kind": kind, "symbol": symbol, **detail,
                           **({"paper_id": paper_id} if paper_id else {}),
                           **({"debate_id": debate_id} if debate_id else {})})

    def start(self, resume: bool) -> AsOf | None:
        """Open a run (or resume the last unfinished one). Returns the last slot done."""
        row = self.conn.execute("SELECT id, last_slot, state_json FROM week_runs WHERE status "
                                "IN ('running', 'stopped', 'failed') ORDER BY id DESC LIMIT 1"
                                ).fetchone() if resume else None
        if row:
            self.run_id = row[0]
            for sym, t in json.loads(row[2] or "{}").items():
                if sym in self.tracked:
                    self.tracked[sym] = Tracked(**t)
                    if t.get("paper_id"):
                        self.packets[sym] = load_packet(self.conn, t["paper_id"])
            with self.conn:
                self.conn.execute("UPDATE week_runs SET status = 'running', updated_at = ? "
                                  "WHERE id = ?", (utcnow(), self.run_id))
            return AsOf.parse(row[1]) if row[1] else None
        with self.conn:
            self.run_id = self.conn.execute(
                "INSERT INTO week_runs (started_at, status, symbols_json, window_json, "
                "updated_at) VALUES (?, 'running', ?, ?, ?)",
                (utcnow(), json.dumps(self.symbols), json.dumps(
                    {"start": self.cfg.week.start.isoformat(), "end": self.cfg.week.end.isoformat(),
                     "cadence_minutes": self.cfg.week.cadence_minutes, "slots": len(self.slots)}),
                 utcnow())).lastrowid
        return None

    def save_state(self, slot: AsOf, status: str = "running", note: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE week_runs SET last_slot = ?, state_json = ?, status = ?, note = "
                "COALESCE(?, note), updated_at = ? WHERE id = ?",
                (slot.iso(), json.dumps({s: asdict(t) for s, t in self.tracked.items()}),
                 status, note, utcnow(), self.run_id))

    def db_bytes(self) -> int:
        path = Path(self.cfg.db_path)
        return sum(p.stat().st_size for p in (path, Path(f"{path}-wal")) if p.exists())

    # -------------------------------------------------- 1. research
    async def research(self, slot: AsOf) -> list[str]:
        """Rewrite the reports that need it. Returns the symbols rewritten."""
        sem = asyncio.Semaphore(RESEARCH_CONCURRENCY)
        prices = {s: price_now(self.cfg, s, slot) for s in self.symbols}
        filings = {s: visible_filings(self.sec, s, slot) for s in self.symbols}
        due = {s: r for s in self.symbols
               if (r := research_reason(self.tracked[s], prices[s], filings[s],
                                        self.cfg.week.price_move_pct))}

        async def one(sym: str, reason: str) -> str | None:
            t = self.tracked[sym]
            question = " | ".join(t.questions) or None
            req = ResearchRequest(symbol=sym, cutoff=slot.iso(), question=question,
                                  requested_by="assessment" if question else "discovery")
            async with sem:
                try:
                    packet, calls = await ResearchAgent(self.cfg).run(req)
                except ResearchFailed as exc:
                    if exc.llm_calls:
                        with self.conn:
                            record_llm_calls(self.conn, "research",
                                             self.cfg.agents["research"].tier, exc.llm_calls)
                    self.event(slot, "error", sym, stage="research", error=str(exc)[:500])
                    return None
            old = t.paper_id
            paper_id = save_packet(self.conn, req, packet, calls,
                                   self.cfg.agents["research"].tier)
            if old and state_of(self.conn, old) == "packet_ready":
                with self.conn:
                    transition(self.conn, old, "dropped", "research",
                               f"superseded by the report at {slot.iso()}")
            self.packets[sym] = packet
            t.paper_id, t.report_at, t.report_price = paper_id, slot.iso(), prices[sym]
            t.filings = [f["accession"] for f in filings[sym]]
            t.answering, t.questions = question, []
            self.event(slot, "research", sym, paper_id=paper_id, reason=reason,
                       price=prices[sym], observations=[o.text for o in packet.observations],
                       gaps=packet.gaps)
            return sym

        done = await asyncio.gather(*(one(s, r) for s, r in due.items()))
        written = [s for s in done if s]
        self.event(slot, "slot", prices=prices, rewritten=written,
                   kept=[s for s in self.symbols if s not in written])
        return written

    # -------------------------------------------------- 2. screen
    async def screen(self, slot: AsOf, portfolio: dict[str, Any]) -> list[str]:
        """Assessment reads every report. Returns the symbols advanced."""
        reports = []
        for sym in self.symbols:
            if sym not in self.packets:
                continue
            t = self.tracked[sym]
            now = price_now(self.cfg, sym, slot)
            seen = set(t.filings)
            reports.append({
                "packet": self.packets[sym], "price_now": now,
                "change_since_report_pct": round((now / t.report_price - 1) * 100, 2)
                if now and t.report_price else None,
                "new_filings_since_report": [f for f in visible_filings(self.sec, sym, slot)
                                             if f["accession"] not in seen],
                "answering": t.answering})
        if not reports:
            return []
        today = slot.day.isoformat()
        debated = [s for s, t in self.tracked.items() if t.debated_day == today]
        try:
            result, _, trace = await self.assessment.screen(reports, portfolio, slot.iso(), debated)
        except AgentFailed as exc:
            self.flush_assessment()
            self.event(slot, "error", stage="screen", error=str(exc)[:500])
            return []
        self.flush_assessment()
        advanced = []
        for item in result.items:
            t = self.tracked[item.symbol]
            decision = item.decision
            if decision == "dig":
                if t.digs.get(today, 0) >= MAX_DIGS_PER_DAY:
                    decision = "hold"
                    item.reason += f" (dig limit of {MAX_DIGS_PER_DAY} a day reached)"
                else:
                    t.digs[today] = t.digs.get(today, 0) + 1
                    t.questions = list(item.questions)
            t.last_decision, t.last_reason = decision, item.reason
            if decision == "advance":
                advanced.append(item.symbol)
        self.event(slot, "screen", items=[{**i.model_dump(), "decision": self.tracked[
            i.symbol].last_decision} for i in result.items], notes=result.notes,
            tools=[{k: v for k, v in x.items() if k != "arguments"} for x in trace])
        return advanced

    def flush_assessment(self, paper_id: int | None = None) -> None:
        if self.assessment.calls:
            with self.conn:
                record_llm_calls(self.conn, "assessment", self.cfg.agents["assessment"].tier,
                                 self.assessment.calls, paper_id)
            self.assessment.calls.clear()

    # -------------------------------------------------- 3. debate
    def due_for_debate(self, sym: str, slot: AsOf) -> str | None:
        """None if it may be debated now, else why not."""
        t = self.tracked[sym]
        if not self.cfg.week.debate_once_per_day or t.debated_day != slot.day.isoformat():
            return None
        new = [f for f in visible_filings(self.sec, sym, slot)
               if f["accession"] not in t.debate_filings]
        return None if new else "already debated today, nothing new filed since"

    async def debate(self, sym: str, slot: AsOf, portfolio: dict[str, Any]) -> None:
        t = self.tracked[sym]
        packet = self.packets[sym]
        paper_id = t.paper_id
        if paper_id is None or state_of(self.conn, paper_id) != "packet_ready":
            # the report was already debated (or held): carry it into a new paper
            req = ResearchRequest(symbol=sym, cutoff=slot.iso(), requested_by="assessment",
                                  question=f"carried over from the report at {t.report_at}")
            paper_id = save_packet(self.conn, req, packet, [], self.cfg.agents["research"].tier)
            t.paper_id = paper_id
        try:
            proposal = await self.assessment.propose(packet, portfolio)
        except AgentFailed as exc:
            self.flush_assessment(paper_id)
            self.event(slot, "error", sym, paper_id=paper_id, stage="proposal",
                       error=str(exc)[:500])
            return
        self.flush_assessment(paper_id)
        proposal_id = save_proposal(self.conn, paper_id, proposal.model_dump(),
                                    verdict=proposal.verdict)
        t.debated_day = slot.day.isoformat()
        t.debate_filings = [f["accession"] for f in visible_filings(self.sec, sym, slot)]
        if proposal.verdict != "submit":
            self.event(slot, "proposal_withdrawn", sym, paper_id=paper_id,
                       verdict=proposal.verdict, thesis=proposal.thesis)
            return
        self.event(slot, "debate_started", sym, paper_id=paper_id, thesis=proposal.thesis,
                   suggested_position_pct=proposal.suggested_position_pct)
        run = await debate_paper(self.cfg, self.conn, packet, portfolio, self.assessment,
                                 proposal, paper_id, proposal_id, save=True)
        self.flush_assessment(paper_id)
        last = run.debate
        self.event(slot, "debate", sym, paper_id=paper_id, debate_id=run.debate_id,
                   outcome=run.outcome, rounds=sum(len(d.rounds) for d in run.debates),
                   size_usd=last.size_usd if last else None,
                   amendment=(last.final.amendment if last and last.final else None))

    # -------------------------------------------------- the loop
    @property
    def control_files(self) -> tuple[Path, Path]:
        """(pid file while running, stop file: create it to stop after the slot)."""
        return Path(f"{self.cfg.db_path}.week.pid"), Path(f"{self.cfg.db_path}.week.stop")

    async def run(self, resume: bool = False, until: str | None = None,
                  max_slots: int | None = None) -> dict[str, Any]:
        import os

        pid_file, stop_file = self.control_files
        stop_file.unlink(missing_ok=True)
        pid_file.write_text(str(os.getpid()))
        try:
            return await self._run(resume, until, max_slots)
        finally:
            pid_file.unlink(missing_ok=True)

    async def _run(self, resume: bool, until: str | None, max_slots: int | None
                   ) -> dict[str, Any]:
        done_slot = self.start(resume)
        slots = [s for s in self.slots if done_slot is None or s.end > done_slot.end]
        if until:
            stop_at = AsOf.parse(until)
            slots = [s for s in slots if s.end <= stop_at.end]
        if max_slots is not None:
            slots = slots[:max_slots]
        day: date | None = None
        day_start_bytes = self.db_bytes()
        limit = self.cfg.week.gb_per_day * 1024 ** 3
        dataset_per_day = json.loads((self.cfg.week.dataset_dir / "manifest.json").read_text()
                                     )["storage"]["bytes_per_market_day"]
        for slot in slots:
            opening = slot.day != day
            if opening:
                day, day_start_bytes = slot.day, self.db_bytes()
            try:
                portfolio = snapshot(self.conn, self.cfg, slot.day).model_dump(mode="json")
                changed = await self.research(slot)
                # Assessment reviews everything at each day's first slot (the open),
                # and whenever a report was rewritten
                if changed or opening:
                    for sym in await self.screen(slot, portfolio):
                        why_not = self.due_for_debate(sym, slot)
                        if why_not:
                            self.event(slot, "debate_skipped", sym, reason=why_not)
                            continue
                        await self.debate(sym, slot, portfolio)
            except Exception as exc:
                self.save_state(slot, "failed", f"{type(exc).__name__}: {exc}"[:500])
                self.event(slot, "error", stage="slot", error=f"{type(exc).__name__}: {exc}")
                raise
            written = self.db_bytes() - day_start_bytes + dataset_per_day
            if written > limit:
                self.save_state(slot, "stopped", f"{day}: wrote {written:,} bytes, over the "
                                                 f"{self.cfg.week.gb_per_day} GB/day limit")
                self.event(slot, "storage", written_bytes=written, limit_bytes=limit)
                return self.summary()
            if self.stop_requested or self.control_files[1].exists():
                self.save_state(slot, "stopped", "stopped on request")
                return self.summary()
            self.save_state(slot)
        last = slots[-1] if slots else self.slots[-1]
        finished = last == self.slots[-1]
        self.save_state(last, "done" if finished else "stopped",
                        None if finished else f"paused after {last.iso()}")
        return self.summary()

    def summary(self) -> dict[str, Any]:
        rows = self.conn.execute("SELECT kind, COUNT(*) FROM week_events WHERE run_id = ? "
                                 "GROUP BY kind", (self.run_id,)).fetchall()
        outcomes = self.conn.execute(
            "SELECT json_extract(detail_json, '$.outcome'), COUNT(*) FROM week_events WHERE "
            "run_id = ? AND kind = 'debate' GROUP BY 1", (self.run_id,)).fetchall()
        status = self.conn.execute("SELECT status, last_slot, note FROM week_runs WHERE id = ?",
                                   (self.run_id,)).fetchone()
        return {"run_id": self.run_id, "status": status[0], "last_slot": status[1],
                "note": status[2], "events": {k: n for k, n in rows},
                "debate_outcomes": {k: n for k, n in outcomes}}
