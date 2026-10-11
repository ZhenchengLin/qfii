"""Benchmark committee models on an identical debate.

Research runs on the scripted fake model, so every model under test debates the
same dossier; only the committee's model changes. Reports, per model:

  first_try_valid    ballots accepted on the first attempt
  valid              ballots accepted after the one retry (the rest abstain)
  sec_per_ballot     wall time per ballot, retries included
  size_violations    first attempts sized above the hard limits
  engages_others     rounds 2+: arguments that mention another member (heuristic)
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .agents.research.agent import ResearchAgent
from .committee.engine import Dossier, run_debate
from .committee.members import build_members
from .committee.runner import PlaceholderTeams, placeholder_proposal
from .config import COMMITTEE, Config, ModelSpec
from .contracts import ResearchRequest
from .db import connect, init_db
from .llm import model_label
from .portfolio import snapshot

_OTHERS = re.compile(r"\b(aggressive|conservative|neutral|others?|colleagues?|members?)\b",
                     re.IGNORECASE)


async def bench_debate(cfg: Config, symbol: str, as_of: date, on_round=None) -> dict[str, Any]:
    fake_research = replace(cfg, models={**cfg.models, "mid": ModelSpec("fake", "fake-mid")})
    packet, _ = await ResearchAgent(fake_research).run(
        ResearchRequest(symbol=symbol, as_of=as_of))
    conn = connect(":memory:")
    init_db(conn, cfg.limits.starting_cash_usd)
    dossier = Dossier(packet.symbol, placeholder_proposal(packet), None,
                      snapshot(conn, cfg, as_of).model_dump(mode="json"))
    members = build_members(cfg)
    started = datetime.now(UTC)
    result = await run_debate(dossier, members, PlaceholderTeams(), cfg.committee,
                              on_round=on_round)
    seconds = (datetime.now(UTC) - started).total_seconds()

    entries = [(m.name, e) for m in members for e in m.ballot_log]
    attempts = [a for _, e in entries for a in e["attempts"]]
    ballot_secs = [sum(a.get("seconds", 0) for a in e["attempts"]) for _, e in entries]
    late = [b for r in result.rounds[1:] for b in r.ballots if b.action != "abstain"]
    models = {m: model_label(cfg.model_for(m)) for m in COMMITTEE}
    return {
        "models": models,
        "symbol": symbol, "as_of": as_of.isoformat(),
        "outcome": result.outcome, "size_usd": result.size_usd,
        "rounds": len(result.rounds), "debate_seconds": round(seconds, 1),
        "ballots": len(entries),
        "first_try_valid": sum(bool(e["attempts"]) and e["attempts"][0]["ok"]
                               for _, e in entries),
        "valid": sum(e["valid"] for _, e in entries),
        "sec_per_ballot": round(statistics.mean(ballot_secs), 1) if ballot_secs else None,
        "size_violations": sum("exceeds largest_allowed" in (a.get("error") or "")
                               for a in attempts),
        "engages_others": f"{sum(bool(_OTHERS.search(b.argument)) for b in late)}/{len(late)}",
        "prompt_tokens": sum(a["prompt_tokens"] for a in attempts),
        "completion_tokens": sum(a["completion_tokens"] for a in attempts),
        "errors": sorted({(a.get("error") or "")[:120] for a in attempts if not a["ok"]}),
        "transcript": [
            {"round": r.round, "result": r.outcome.kind, "detail": r.outcome.detail,
             "ballots": [b.model_dump(exclude_none=True) for b in r.ballots]}
            for r in result.rounds],
    }


def save_report(report: dict[str, Any], out_dir: Path, label: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{label}-{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    return path


def format_table(reports: list[dict[str, Any]]) -> str:
    rows = [("model (neutral)", "outcome", "rounds", "first-try valid", "valid",
             "s/ballot", "size viol.", "engages others", "tokens in/out")]
    for r in reports:
        rows.append((r["models"]["neutral"], f"{r['outcome']}"
                     + (f" ${r['size_usd']:,.0f}" if r["size_usd"] else ""),
                     str(r["rounds"]), f"{r['first_try_valid']}/{r['ballots']}",
                     f"{r['valid']}/{r['ballots']}", str(r["sec_per_ballot"]),
                     str(r["size_violations"]), r["engages_others"],
                     f"{r['prompt_tokens']}/{r['completion_tokens']}"))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(row, widths)) for row in rows)
