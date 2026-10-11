"""Scripted stand-ins for the committee LLMs, so a full debate runs offline.

Not a strategy: each persona starts from a different size preference, and they
converge on neutral's anchor over the rounds, so demos show argument, an
information request, and size negotiation.
"""

from __future__ import annotations

import json
from collections.abc import Callable

# fraction of the largest allowed order each persona would like to buy
_PREFERENCE = {"aggressive": 0.9, "conservative": 0.4, "neutral": 0.6}
# how far each member still holds out from neutral's anchor, by round
_HOLDOUT = {1: 1.0, 2: 0.5}


def _max_order(portfolio: dict) -> float:
    from ..portfolio import max_new_order_usd

    return max_new_order_usd(portfolio)


def fake_member_responder(member: str) -> Callable[[str, str], str]:
    def respond(system: str, user: str) -> str:
        from .members import read_member_prompt

        view = read_member_prompt(user)
        r = len(view["previous_rounds"]) + 1
        cap = _max_order(view["portfolio"])
        if cap < 100:
            return json.dumps({"action": "pass",
                               "argument": f"No room under cash and position limits "
                                           f"(max order ${cap:,.0f})."})
        if r == 1 and member != "aggressive" and not view["additional_reports"]:
            return json.dumps({
                "action": "request_info", "target": "risk",
                "argument": f"As {member}, I want the downside quantified before sizing.",
                "request": "What is the drawdown and liquidity risk at a full-size position?",
            })
        anchor = _PREFERENCE["neutral"] * cap
        holdout = _HOLDOUT.get(r, 0.0)
        size = round((anchor + holdout * (_PREFERENCE[member] * cap - anchor)) / 100) * 100
        others = [b for rnd in view["previous_rounds"] for b in rnd["ballots"]
                  if b["member"] != member and b.get("size_usd")]
        mine = [b for rnd in view["previous_rounds"] for b in rnd["ballots"]
                if b["member"] == member]
        reply = {
            "action": "invest", "size_usd": size,
            "argument": f"As {member}, I propose ${size:,.0f}"
                        + (f", moving toward the others' {[o['size_usd'] for o in others]}."
                           if others else "."),
        }
        if mine and (mine[-1]["action"] != "invest" or mine[-1].get("size_usd") != size):
            reply["changed_because"] = "the other members' sizes and the risk report"
        if r >= 3:
            reply["note_to_self"] = f"{view['symbol']}: settled near ${anchor:,.0f}."
        return json.dumps(reply)

    return respond
