"""Committee vote counting. Pure, deterministic code: no LLM decides the outcome.

Three members, equal weight. Each round every member casts one ballot:

  invest        open a new position at size_usd
  replace       sell replace_symbol, buy the new stock at size_usd
  pass          do not invest
  request_info  ask a team (assessment / risk / both) for a report everyone sees
  send_back     return the paper to a team for full revision, with feedback
  propose       put an amended deal on the table (size + terms); counts as backing it
  support       back an amendment already on the table, by its id (A1, A2, ...)

Trading actions invest and replace need `invest_rule` votes, pass needs
`pass_rule` votes; invest and replace also need agreement on the size (all within size_tolerance_pct; the
median is used) and, for replace, on which position to sell. Backing the same
amendment (propose + support) also needs `invest_rule` votes; its terms are
fixed, so that is agreement on the size and terms. Procedural actions
send_back needs `procedural_rule` votes; request_info needs `info_request_rule`
("any": one member's question is answered). Anything else: no
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
    amendment_id: str | None = None
    amendment: dict | None = None          # agreed terms when the outcome is an amendment
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


def tally(ballots: list[Ballot], rules: Committee,
          amendments: dict[str, dict] | None = None) -> RoundOutcome:
    """amendments: id -> terms for every amendment on the table (including ones
    proposed this round, whose ballots carry their assigned amendment_id)."""
    amendments = amendments or {}
    counts = Counter(b.action for b in ballots)
    votes = dict(counts)
    by_action: dict[str, list[Ballot]] = {}
    for b in ballots:
        by_action.setdefault(b.action, []).append(b)

    trade_need = votes_needed(rules.invest_rule, len(ballots))
    pass_need = votes_needed(rules.pass_rule, len(ballots))
    blocked: list[str] = []
    for action in TRADE_ACTIONS:
        group = by_action.get(action, [])
        if len(group) < (pass_need if action == "pass" else trade_need):
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

    backers: dict[str, list[Ballot]] = {}
    for b in ballots:
        if b.action in ("propose", "support") and b.amendment_id:
            backers.setdefault(b.amendment_id, []).append(b)
    for aid, group in sorted(backers.items(), key=lambda kv: -len(kv[1])):
        if len(group) >= trade_need and aid in amendments:
            terms = amendments[aid]
            return RoundOutcome("invest", votes, size_usd=terms["size_usd"], amendment_id=aid,
                                amendment=terms,
                                detail=f"{len(group)} of {len(ballots)} back amendment {aid}")
    if backers:
        blocked.append("amendment backing: " + ", ".join(
            f"{aid} by {'/'.join(b.member for b in g)}" for aid, g in sorted(backers.items())))

    proc_need = votes_needed(rules.procedural_rule, len(ballots))
    backs = by_action.get("send_back", [])
    if len(backs) >= proc_need:
        return RoundOutcome("send_back", votes, teams=_teams(backs),
                            requests=[f"[{b.member}] {b.request}" for b in backs],
                            detail=f"{len(backs)} of {len(ballots)} send_back")

    # Questions to a team: with info_request_rule "any", one member's question is
    # answered and the report goes to everyone.
    asks = by_action.get("request_info", [])
    info_need = 1 if rules.info_request_rule == "any" else \
        votes_needed(rules.info_request_rule, len(ballots))
    if asks and len(asks) >= info_need:
        return RoundOutcome("request_info", votes, teams=_teams(asks),
                            requests=[f"[{b.member}] {b.request}" for b in asks],
                            detail=f"{len(asks)} of {len(ballots)} asked for information")

    return RoundOutcome("continue", votes, detail="; ".join(blocked) or "no action reached "
                        f"its threshold (trade needs {trade_need}, pass needs {pass_need}, "
                        f"procedural needs {proc_need})")
