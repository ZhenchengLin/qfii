"""Evaluate the whole department on many stocks: how many rounds it takes the
committee to reach a conclusion, how often it concludes, and where it struggles.

Each stock runs the real pipeline (research -> Assessment -> committee, with team
reports on request) and is saved like any debate. Per stock we record:

  outcome            invest / replace / pass / no_consensus / assessment_* / error
  concluded          the committee reached a decision (invest, replace or pass)
  rounds_to_conclusion  rounds of the deciding debate (only when concluded)
  rounds_total       rounds across all debates for the paper (send-backs included)
  info_requests      team reports asked for; report_failures among them
  amendments         amendments proposed; decided_on_amendment
  vote_changes       ballots with a changed_because; abstentions (invalid ballots)
  final_votes        the last round's ballots by action (who was holding out)
  seconds            wall time for the paper
  what_if            the same ballots re-counted under other rules (see replay.py):
                     how many rounds and which outcome, with no extra LLM calls
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import time
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .committee.replay import from_records, load_paper, replay_paper
from .committee.runner import run_paper
from .config import Committee, Config
from .db import connect, init_db
from .tools.client import root_cause

CONCLUDED = ("invest", "replace", "pass")

# Rule variants measured by replaying the real ballots (no LLM calls).
WHAT_IF: dict[str, dict[str, Any]] = {
    "pass_by_majority": {"pass_rule": "majority"},
    "stop_after_3_unchanged": {"stop_after_unchanged_rounds": 3},
    "majority_pass_after_2_unchanged": {"majority_pass_after_unchanged_rounds": 2},
    "majority_pass_after_3_unchanged": {"majority_pass_after_unchanged_rounds": 3},
}


def what_if(debates: list, rules: Committee) -> dict[str, dict[str, Any]]:
    """debates: [(rounds of ballots, actual round outcomes)] for one paper."""
    if not debates:
        return {}
    return {name: asdict(replay_paper(debates, replace(rules, **change)))
            for name, change in WHAT_IF.items()}


def _final_votes(debates: list) -> dict[str, int]:
    if not debates or not debates[-1].rounds:
        return {}
    votes: dict[str, int] = {}
    for b in debates[-1].rounds[-1].ballots:
        votes[b.action] = votes.get(b.action, 0) + 1
    return votes


def paper_metrics(symbol: str, run: Any, seconds: float,
                  rules: Committee | None = None) -> dict[str, Any]:
    debates = run.debates
    ballots = [b for d in debates for r in d.rounds for b in r.ballots]
    reports = [rep for d in debates for r in d.rounds for rep in r.reports]
    last = debates[-1] if debates else None
    concluded = run.outcome in CONCLUDED
    return {
        "symbol": symbol,
        "outcome": run.outcome,
        "concluded": concluded,
        "rounds_to_conclusion": len(last.rounds) if concluded and last else None,
        "rounds_total": sum(len(d.rounds) for d in debates),
        "debates": len(debates),
        "size_usd": last.size_usd if last and concluded else None,
        "decided_on_amendment": bool(last and last.final and last.final.amendment_id),
        "assessment_verdict": run.proposal.verdict if run.proposal else None,
        "info_requests": len(reports),
        "report_failures": sum(1 for rep in reports if (rep.get("report") or {}).get("error")),
        "amendments": sum(1 for b in ballots if b.action == "propose"),
        "vote_changes": sum(1 for b in ballots if b.changed_because),
        "abstentions": sum(1 for b in ballots if b.action == "abstain"),
        "ballots_by_action": {a: sum(1 for b in ballots if b.action == a)
                              for a in sorted({b.action for b in ballots})},
        "final_votes": _final_votes(debates),
        "seconds": round(seconds, 1),
        "error": None,
        "what_if": what_if([from_records(d.rounds) for d in debates], rules) if rules else {},
    }


def summarize(rows: list[dict[str, Any]], max_rounds: int) -> dict[str, Any]:
    ok = [r for r in rows if not r["error"]]
    debated = [r for r in ok if r["debates"]]
    concluded = [r for r in debated if r["concluded"]]
    rounds = [r["rounds_to_conclusion"] for r in concluded]

    def mean(xs: list[float | None]) -> float | None:
        xs = [x for x in xs if x is not None]
        return round(statistics.mean(xs), 2) if xs else None

    outcomes: dict[str, int] = {}
    for r in rows:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    return {
        "papers": len(rows),
        "errors": len(rows) - len(ok),
        "reached_committee": len(debated),
        "concluded": len(concluded),
        "conclusion_rate": round(len(concluded) / len(debated), 3) if debated else None,
        "avg_rounds_to_conclusion": mean(rounds),
        "median_rounds_to_conclusion": statistics.median(rounds) if rounds else None,
        "min_max_rounds_to_conclusion": [min(rounds), max(rounds)] if rounds else None,
        # no-consensus papers ran all rounds: an upper bound, reported separately
        "avg_rounds_all_debated": mean([r["rounds_total"] for r in debated]),
        "max_rounds_setting": max_rounds,
        "outcomes": outcomes,
        "decided_on_amendment": sum(r["decided_on_amendment"] for r in ok),
        "info_requests": sum(r["info_requests"] for r in ok),
        "report_failures": sum(r["report_failures"] for r in ok),
        "amendments": sum(r["amendments"] for r in ok),
        "vote_changes": sum(r["vote_changes"] for r in ok),
        "abstentions": sum(r["abstentions"] for r in ok),
        "avg_seconds_per_paper": mean([r["seconds"] for r in ok]),
        "what_if": _summarize_what_if(debated),
    }


def _summarize_what_if(debated: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in WHAT_IF:
        reps = [r["what_if"][name] for r in debated if (r.get("what_if") or {}).get(name)]
        if not reps:
            continue
        done = [x for x in reps if x["outcome"] in CONCLUDED]
        outcomes: dict[str, int] = {}
        for x in reps:
            outcomes[x["outcome"]] = outcomes.get(x["outcome"], 0) + 1
        out[name] = {
            "concluded": len(done),
            "conclusion_rate": round(len(done) / len(reps), 3),
            "avg_rounds_to_conclusion": (round(statistics.mean(x["rounds"] for x in done), 2)
                                         if done else None),
            "avg_rounds_all_debated": round(statistics.mean(x["rounds"] for x in reps), 2),
            "rounds_saved": sum(r["rounds_total"] for r in debated
                                if (r.get("what_if") or {}).get(name))
                            - sum(x["rounds"] for x in reps),
            "outcomes": outcomes,
            "inexact": sum(1 for x in reps if not x["exact"]),
        }
    return out


def markdown_report(rows: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    s = summary
    lines = [
        "# Qfii evaluation",
        "",
        (f"- Papers: {s['papers']} ({s['errors']} errors); reached the committee: "
        f"{s['reached_committee']}"),
        f"- Concluded: {s['concluded']} (rate {s['conclusion_rate']})",
        (f"- **Average rounds to conclusion: {s['avg_rounds_to_conclusion']}** "
        f"(median {s['median_rounds_to_conclusion']}, range "
        f"{s['min_max_rounds_to_conclusion']}; limit {s['max_rounds_setting']})"),
        (f"- Average rounds per debated paper, including no-consensus: "
        f"{s['avg_rounds_all_debated']}"),
        f"- Outcomes: {s['outcomes']}",
        f"- Team reports: {s['info_requests']} requested, {s['report_failures']} failed",
        (f"- Amendments proposed: {s['amendments']}; decided on an amendment: "
        f"{s['decided_on_amendment']}; vote changes: {s['vote_changes']}; "
        f"invalid ballots: {s['abstentions']}"),
        f"- Average time per paper: {s['avg_seconds_per_paper']} s",
        "",
        ("| symbol | outcome | rounds | debates | size | reports (failed) | amendments "
        "| changes | abstain | seconds |"),
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    tail = []
    if s.get("what_if"):
        tail += ["", "## What if: the same ballots re-counted under other rules", "",
                 "Exact up to the first round whose outcome would differ (no LLM calls).", "",
                 ("| rules | concluded | avg rounds to conclusion | avg rounds per paper "
                 "| rounds saved | outcomes | inexact |"), "|---|---|---|---|---|---|---|",
                 (f"| as run | {s['concluded']} | {s['avg_rounds_to_conclusion']} "
                  f"| {s['avg_rounds_all_debated']} | 0 | {s['outcomes']} | 0 |")]
        for name, w in s["what_if"].items():
            tail.append(f"| {name} | {w['concluded']} | {w['avg_rounds_to_conclusion']} "
                        f"| {w['avg_rounds_all_debated']} | {w['rounds_saved']} "
                        f"| {w['outcomes']} | {w['inexact']} |")
    holdouts = [r for r in rows if r.get("outcome") == "no_consensus" and r.get("final_votes")]
    if holdouts:
        tail += ["", "## No consensus: final positions", ""]
        tail += [f"- {r['symbol']}: {r['final_votes']}" for r in holdouts]
    for r in rows:
        size = f"${r['size_usd']:,.0f}" if r.get("size_usd") else ""
        lines.append(
            f"| {r['symbol']} | {r['outcome']}{' (amendment)' if r.get('decided_on_amendment') else ''}"
            f" | {r.get('rounds_total', '')} | {r.get('debates', '')} | {size} "
            f"| {r.get('info_requests', 0)} ({r.get('report_failures', 0)}) "
            f"| {r.get('amendments', 0)} | {r.get('vote_changes', 0)} "
            f"| {r.get('abstentions', 0)} | {r.get('seconds', '')} |"
            + (f" error: {r['error']}" if r.get("error") else ""))
    return "\n".join(lines + tail) + "\n"


async def evaluate(cfg: Config, symbols: list[str], as_of: date | None, out_dir: Path,
                   on_paper=None) -> dict[str, Any]:
    """Run every symbol through the pipeline, saving results as they arrive."""
    conn = connect(cfg.db_path)
    init_db(conn, cfg.limits.starting_cash_usd)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = out_dir / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        started = time.monotonic()
        try:
            run = await run_paper(cfg, conn, symbol, as_of, save=True)
            row = paper_metrics(symbol, run, time.monotonic() - started, cfg.committee)
        except Exception as exc:  # noqa: BLE001 - one bad stock must not stop the evaluation
            cause = root_cause(exc)
            row = {"symbol": symbol, "outcome": "error", "concluded": False, "debates": 0,
                   "rounds_total": 0, "seconds": round(time.monotonic() - started, 1),
                   "error": f"{type(cause).__name__}: {cause}"[:400]}
        rows.append(row)
        with (run_dir / "papers.jsonl").open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        summary = summarize(rows, cfg.committee.max_rounds)
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        (run_dir / "report.md").write_text(markdown_report(rows, summary))
        if on_paper:
            on_paper(row, summary)
    return {"dir": str(run_dir), "summary": summarize(rows, cfg.committee.max_rounds),
            "papers": rows}


def replay_saved(conn: sqlite3.Connection, rules: Committee, paper_ids: list[int]
                 ) -> dict[str, Any]:
    """Measure saved papers (e.g. an earlier eval) and their what-ifs from the
    database alone. `rules` must be the rules the debates ran with."""
    rows = []
    for pid in paper_ids:
        sym = conn.execute("SELECT symbol FROM papers WHERE id = ?", (pid,)).fetchone()
        debates = load_paper(conn, pid, rules)
        if sym is None or not debates:
            continue
        actual = replay_paper(debates, rules)
        ballots = [b for rounds, _ in debates for r in rounds for b in r]
        n_reports, n_failed = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN json_extract(i.report_json, '$.error') IS NOT NULL "
            "THEN 1 ELSE 0 END) FROM info_requests i JOIN debates d ON d.id = i.debate_id "
            "WHERE d.paper_id = ?", (pid,)).fetchone()
        last_round = debates[-1][0][-1]
        concluded = actual.outcome in CONCLUDED
        rows.append({
            "symbol": sym[0], "paper_id": pid, "outcome": actual.outcome,
            "concluded": concluded,
            "rounds_to_conclusion": len(debates[-1][0]) if concluded else None,
            "rounds_total": actual.rounds, "debates": len(debates),
            "size_usd": actual.size_usd, "decided_on_amendment": bool(actual.amendment_id),
            "info_requests": n_reports or 0, "report_failures": n_failed or 0,
            "amendments": sum(1 for b in ballots if b.action == "propose"),
            "vote_changes": sum(1 for b in ballots if b.changed_because),
            "abstentions": sum(1 for b in ballots if b.action == "abstain"),
            "final_votes": {a: sum(1 for b in last_round if b.action == a)
                            for a in sorted({b.action for b in last_round})},
            "seconds": None, "error": None, "what_if": what_if(debates, rules),
        })
    return {"summary": summarize(rows, rules.max_rounds), "papers": rows}
