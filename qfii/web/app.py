"""Debate viewer: watch the committee argue, live, in a browser.

  GET  /                        the single-page UI
  GET  /api/status              models, portfolio, investor, pipeline counts
  GET  /api/debates             past debates
  GET  /api/debates/{id}        one debate: research, rounds, ballots, reports, outcome
  POST /api/runs                {"symbol": "NVDA", "as_of": "2026-10-09"} starts a debate
  GET  /api/runs/{id}/events    Server-Sent Events: research, ballot, round, outcome, error

One debate runs at a time (they share the models). Events are kept per run, so a
page opened mid-debate replays what happened so far, then continues live.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import date
from importlib import resources
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ..config import COMMITTEE, Config
from ..contracts import ResearchRequest
from ..db import cash_balance_cents, connect, init_db
from ..investor import get_profile
from ..llm import model_label
from ..papers import count_by_state
from ..portfolio import max_new_order_usd, snapshot


@dataclass
class Run:
    id: str
    symbol: str
    as_of: str
    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        self.changed.set()


def _ballot_json(b: Any) -> dict[str, Any]:
    return b.model_dump(exclude_none=True, exclude={"note_to_self"})


class DebateServer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.conn = connect(cfg.db_path)
        init_db(self.conn, cfg.limits.starting_cash_usd)
        self.runs: dict[str, Run] = {}
        self.active: Run | None = None

    # ------------------------------------------------------------ reads
    def status(self) -> dict[str, Any]:
        cfg = self.cfg
        snap = snapshot(self.conn, cfg, date.today()).model_dump(mode="json")
        profile = get_profile(self.conn)
        return {
            "models": {a: model_label(cfg.model_for(a)) for a in ("research", *COMMITTEE)},
            "cash_usd": cash_balance_cents(self.conn) / 100,
            "largest_allowed_order_usd": max_new_order_usd(snap),
            "holdings": snap["holdings"],
            "tax": {k: snap["tax"][k] for k in ("short_term_pct", "long_term_pct", "assumed")},
            "investor_set": profile is not None,
            "papers": count_by_state(self.conn),
            "committee": {"max_rounds": cfg.committee.max_rounds,
                          "trade_rule": cfg.committee.invest_rule,
                          "pass_rule": cfg.committee.pass_rule,
                          "procedural_rule": cfg.committee.procedural_rule},
            "active_run": self.active.id if self.active and not self.active.done else None,
        }

    def debates(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT d.id, p.symbol, d.outcome, d.rounds_used, d.started_at, x.size_usd "
            "FROM debates d JOIN papers p ON p.id = d.paper_id "
            "LEFT JOIN decisions x ON x.debate_id = d.id ORDER BY d.id DESC LIMIT 100")
        return [dict(zip(("id", "symbol", "outcome", "rounds", "started_at", "size_usd"), r))
                for r in rows]

    def debate(self, debate_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT d.id, d.paper_id, p.symbol, d.outcome, d.rounds_used, d.started_at, "
            "x.size_usd, pr.body_json FROM debates d JOIN papers p ON p.id = d.paper_id "
            "LEFT JOIN decisions x ON x.debate_id = d.id "
            "LEFT JOIN proposals pr ON pr.id = d.proposal_id WHERE d.id = ?", (debate_id,)
        ).fetchone()
        if row is None:
            return None
        did, paper_id, symbol, outcome, rounds_used, started, size, proposal_json = row
        packet = self.conn.execute("SELECT body_json FROM packets WHERE paper_id = ? "
                                   "ORDER BY id DESC LIMIT 1", (paper_id,)).fetchone()
        body = json.loads(packet[0]) if packet else {}
        rounds: dict[int, dict[str, Any]] = {}
        amendments: dict[str, dict[str, Any]] = {}
        for (rnd, member, action, size_usd, repl, target, request, argument, aid,
             ajson, changed) in self.conn.execute(
                "SELECT round, member, action, size_usd, replace_symbol, target, request, "
                "argument, amendment_id, amendment_json, changed_because FROM ballots "
                "WHERE debate_id = ? ORDER BY round, id", (did,)):
            ballot = {"member": member, "action": action, "argument": argument}
            ballot.update({k: v for k, v in (("size_usd", size_usd), ("replace_symbol", repl),
                                             ("target", target), ("request", request),
                                             ("amendment_id", aid),
                                             ("changed_because", changed)) if v})
            if ajson:
                ballot["amendment"] = json.loads(ajson)
                amendments[aid] = {**ballot["amendment"], "proposed_by": member,
                                   "proposed_in_round": rnd}
            rounds.setdefault(rnd, {"round": rnd, "ballots": [], "reports": []})
            rounds[rnd]["ballots"].append(ballot)
        for rnd, team, questions, report in self.conn.execute(
                "SELECT round, team, questions, report_json FROM info_requests "
                "WHERE debate_id = ? ORDER BY id", (did,)):
            rounds.setdefault(rnd, {"round": rnd, "ballots": [], "reports": []})
            rounds[rnd]["reports"].append({"team": team, "requests": json.loads(questions),
                                           "report": json.loads(report or "null")})
        stored = self.conn.execute("SELECT round_results_json FROM debates WHERE id = ?",
                                   (did,)).fetchone()[0]
        if stored:
            for i, res in enumerate(json.loads(stored), start=1):
                if i in rounds:
                    rounds[i]["result"], rounds[i]["detail"] = res["result"], res["detail"]
        # debates saved before v7 have no stored results: vote counting is
        # deterministic, so recompute them with the current rules
        from ..committee.protocol import tally
        from ..contracts import Ballot

        for rnd in ([] if stored else rounds.values()):
            ballots = [Ballot(**b) for b in rnd["ballots"]]
            for b, raw in zip(ballots, rnd["ballots"], strict=True):
                b.amendment_id = raw.get("amendment_id")   # engine-assigned on propose
            if len(ballots) == len(COMMITTEE):
                result = tally(ballots, self.cfg.committee, amendments)
                rnd["result"], rnd["detail"] = result.kind, result.detail
        last = rounds.get(rounds_used)
        if not stored and last and outcome != "no_consensus" and last.get("result") != outcome:
            # the debate ran under other rules (e.g. a stalemate majority pass)
            last["result"] = outcome
            last["detail"] = "decided under the committee rules in force at the time"
        return {
            "id": did, "symbol": symbol, "outcome": outcome, "rounds_used": rounds_used,
            "started_at": started, "size_usd": size,
            "proposal": json.loads(proposal_json) if proposal_json else None,
            "amendments": [{"id": k, **v} for k, v in amendments.items()],
            "decision_terms": self._decision_terms(did),
            "research": {"as_of": body.get("as_of"),
                         "observations": body.get("observations", []),
                         "flags": body.get("quality_flags", [])},
            "rounds": [rounds[k] for k in sorted(rounds)],
        }

    def _decision_terms(self, debate_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT amendment_id, terms_json FROM decisions "
                                "WHERE debate_id = ?", (debate_id,)).fetchone()
        if not row or not row[0]:
            return None
        return {"amendment_id": row[0], **json.loads(row[1] or "{}")}

    # ------------------------------------------------------------ live runs
    def start(self, symbol: str, as_of: date | None) -> Run:
        if self.active and not self.active.done:
            raise RuntimeError(f"a debate on {self.active.symbol} is already running")
        req = ResearchRequest(symbol=symbol, as_of=as_of)  # validates the ticker
        run = Run(uuid.uuid4().hex[:12], req.symbol, (as_of or date.today()).isoformat())
        self.runs[run.id] = run
        self.active = run
        asyncio.get_running_loop().create_task(self._execute(run, as_of))
        return run

    async def _execute(self, run: Run, as_of: date | None) -> None:
        from ..committee.runner import run_paper

        run.emit({"type": "started", "symbol": run.symbol, "as_of": run.as_of,
                  "members": list(COMMITTEE),
                  "models": {m: model_label(self.cfg.model_for(m)) for m in COMMITTEE}})

        def on_research(packet) -> None:
            run.emit({"type": "research", "as_of": packet.as_of, "flags": packet.quality_flags,
                      "observations": [{"specialty": o.specialty, "text": o.text}
                                       for o in packet.observations]})

        def on_ballot(symbol: str, rnd: int, ballot) -> None:
            run.emit({"type": "ballot", "round": rnd, "ballot": _ballot_json(ballot)})

        def on_round(symbol: str, rec) -> None:
            run.emit({"type": "round", "round": rec.round, "result": rec.outcome.kind,
                      "detail": rec.outcome.detail, "votes": rec.outcome.votes,
                      "size_usd": rec.outcome.size_usd,
                      "amendment_id": rec.outcome.amendment_id,
                      "amendment": rec.outcome.amendment,
                      "reports": [{"team": r["team"], "requests": r["requests"],
                                   "report": r["report"]} for r in rec.reports]})

        def on_proposal(proposal, version: int) -> None:
            run.emit({"type": "proposal", "version": version, "proposal": proposal.model_dump()})

        try:
            result = await run_paper(self.cfg, self.conn, run.symbol, as_of, save=True,
                                     on_research=on_research, on_round=on_round,
                                     on_ballot=on_ballot, on_proposal=on_proposal)
            last = result.debate
            run.emit({"type": "outcome", "outcome": result.outcome,
                      "size_usd": last.size_usd if last else None,
                      "terms": ({"amendment_id": last.final.amendment_id, **last.final.amendment}
                                if last and last.final and last.final.amendment else None),
                      "rounds": sum(len(d.rounds) for d in result.debates),
                      "debate_id": result.debate_id})
        except Exception as exc:  # noqa: BLE001 - report any failure to the page
            from ..tools.client import root_cause

            cause = root_cause(exc)
            run.emit({"type": "error", "message": str(cause) or type(cause).__name__})
        finally:
            run.done = True
            run.changed.set()

    async def stream(self, run: Run):
        sent = 0
        while True:
            while sent < len(run.events):
                yield f"data: {json.dumps(run.events[sent], default=str)}\n\n"
                sent += 1
            if run.done:
                yield "event: end\ndata: {}\n\n"
                return
            run.changed.clear()
            try:
                await asyncio.wait_for(run.changed.wait(), timeout=15)
            except TimeoutError:
                yield ": keep-alive\n\n"


def build_app(cfg: Config) -> Starlette:
    server = DebateServer(cfg)
    page = resources.files("qfii.web").joinpath("static/index.html").read_text()

    async def index(request: Request) -> Response:
        return HTMLResponse(page)

    async def status(request: Request) -> Response:
        return JSONResponse(server.status())

    async def debates(request: Request) -> Response:
        return JSONResponse(server.debates())

    async def debate(request: Request) -> Response:
        data = server.debate(int(request.path_params["id"]))
        return JSONResponse(data) if data else JSONResponse({"error": "not found"}, 404)

    async def start_run(request: Request) -> Response:
        try:
            body = await request.json()
            as_of = date.fromisoformat(body["as_of"]) if body.get("as_of") else None
            run = server.start(str(body.get("symbol", "")), as_of)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, 409)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            return JSONResponse({"error": f"bad request: {exc}"}, 400)
        return JSONResponse({"run_id": run.id, "symbol": run.symbol, "as_of": run.as_of})

    async def events(request: Request) -> Response:
        run = server.runs.get(request.path_params["id"])
        if run is None:
            return JSONResponse({"error": "no such run"}, 404)
        return StreamingResponse(server.stream(run), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    app = Starlette(routes=[
        Route("/", index),
        Route("/api/status", status),
        Route("/api/debates", debates),
        Route("/api/debates/{id:int}", debate),
        Route("/api/runs", start_run, methods=["POST"]),
        Route("/api/runs/{id}/events", events),
    ])
    app.state.server = server
    return app
