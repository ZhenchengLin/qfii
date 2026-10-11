"""Committee vote counting. Pure, deterministic code: no LLM decides the outcome.

Three members, equal weight. Each round every member casts one ballot:

  invest        open a new position at size_usd
  replace       sell replace_symbol, buy the new stock at size_usd
  pass          do not invest
  request_info  ask a team (assessment / risk / both) for a report everyone sees
  send_back     return the paper to a team for full revision, with feedback

Trading actions (invest, replace, pass) need `invest_rule` votes; invest and
replace also need agreement on the size (all within size_tolerance_pct; the
median is used) and, for replace, on which position to sell. Procedural actions
(request_info, send_back) need `procedural_rule` votes. Anything else: no
decision this round.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field

from ..config import Committee
from ..contracts import Ballot

TRADE_ACTIONS = ("invest", "replace", "pass")
PROCEDURAL_ACTIONS = ("send_back", "request_info")


@dataclass
class RoundOutcome:
    kind: str                       # invest | replace | pass | request_info | send_back | continue
    votes: dict[str, int]
    size_usd: float | None = None
    replace_symbol: str | None = None
    teams: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    detail: str = ""


def votes_needed(rule: str, members: int = 3) -> int:
    return members if rule == "unanimous" else members // 2 + 1


def sizes_agree(sizes: list[float], tolerance_pct: float) -> bool:
    mid = statistics.median(sizes)
    return (max(sizes) - min(sizes)) / mid * 100 <= tolerance_pct + 1e-9


def _teams(ballots: list[Ballot]) -> list[str]:
    teams: set[str] = set()
    for b in ballots:
        teams |= {"assessment", "risk"} if b.target == "both" else {b.target}
    return sorted(teams)


def tally(ballots: list[Ballot], rules: Committee) -> RoundOutcome:
    counts = Counter(b.action for b in ballots)
    votes = dict(counts)
    by_action: dict[str, list[Ballot]] = {}
    for b in ballots:
        by_action.setdefault(b.action, []).append(b)

    trade_need = votes_needed(rules.invest_rule, len(ballots))
    blocked: list[str] = []
    for action in TRADE_ACTIONS:
        group = by_action.get(action, [])
        if len(group) < trade_need:
            continue
        if action == "pass":
            return RoundOutcome("pass", votes, detail=f"{len(group)} of {len(ballots)} pass")
        if action == "replace":
            symbols = {b.replace_symbol for b in group}
            if len(symbols) > 1:
                blocked.append(f"replace agreed but not which position to sell: {sorted(symbols)}")
                continue
        sizes = [b.size_usd for b in group]
        if not sizes_agree(sizes, rules.size_tolerance_pct):
            blocked.append(f"{action} agreed but sizes differ beyond "
                           f"{rules.size_tolerance_pct}%: {sorted(sizes)}")
            continue
        return RoundOutcome(action, votes, size_usd=round(statistics.median(sizes), 2),
                            replace_symbol=group[0].replace_symbol,
                            detail=f"{len(group)} of {len(ballots)} {action}, sizes {sorted(sizes)}")

    proc_need = votes_needed(rules.procedural_rule, len(ballots))
    for action in PROCEDURAL_ACTIONS:
        group = by_action.get(action, [])
        if len(group) >= proc_need:
            return RoundOutcome(action, votes, teams=_teams(group),
                                requests=[f"[{b.member}] {b.request}" for b in group],
                                detail=f"{len(group)} of {len(ballots)} {action}")

    return RoundOutcome("continue", votes, detail="; ".join(blocked) or "no action reached "
                        f"its threshold (trade needs {trade_need}, procedural needs {proc_need})")
