"""Randomized checks of the vote-counting rules over thousands of ballot combinations."""

import random
from dataclasses import replace

import pytest

from qfii.committee.protocol import sizes_agree, tally, votes_needed
from qfii.config import load_config
from qfii.contracts import Ballot

MEMBERS = ("aggressive", "conservative", "neutral")
BASE = load_config().committee


def random_ballot(rng: random.Random, member: str, on_table: list[str]) -> Ballot:
    action = rng.choice(["invest", "pass", "request_info", "send_back", "abstain", "replace",
                         "propose", "support"])
    kw: dict = {}
    if action in ("invest", "replace"):
        kw["size_usd"] = rng.choice([4000, 5000, 5200, 6000, 9000])
    if action == "replace":
        kw["replace_symbol"] = rng.choice(["MSFT", "JPM"])
    if action in ("request_info", "send_back"):
        kw.update(target=rng.choice(["assessment", "risk", "both"]), request="q")
    if action == "propose":
        kw["amendment"] = {"size_usd": rng.choice([3000, 4000]), "summary": "s"}
    if action == "support":
        kw["amendment_id"] = rng.choice(on_table + ["A9"]) if on_table else "A9"
    b = Ballot(member=member, action=action, argument="x", **kw)
    if action == "propose":
        b.amendment_id = f"A{len(on_table) + 1 + MEMBERS.index(member)}"
    return b


@pytest.mark.parametrize("rule", ["unanimous", "majority"])
@pytest.mark.parametrize("pass_rule", ["unanimous", "majority"])
@pytest.mark.parametrize("info_rule", ["any", "majority"])
def test_tally_invariants(rule, pass_rule, info_rule):
    rng = random.Random(f"{rule}-{pass_rule}-{info_rule}")
    rules = replace(BASE, invest_rule=rule, pass_rule=pass_rule, info_request_rule=info_rule)
    need = votes_needed(rule)
    pass_need = votes_needed(pass_rule)
    for _ in range(3000):
        on_table = rng.sample(["A1", "A2"], rng.randint(0, 2))
        ballots = [random_ballot(rng, m, on_table) for m in MEMBERS]
        amendments = {aid: {"size_usd": 3500.0, "summary": "s"} for aid in on_table}
        for b in ballots:
            if b.action == "propose":
                amendments[b.amendment_id] = {"size_usd": b.amendment.size_usd, "summary": "s"}
        out = tally(ballots, rules, amendments)
        actions = [b.action for b in ballots]

        if out.kind in ("invest", "replace") and out.amendment_id is None:
            group = [b for b in ballots if b.action == out.kind]
            assert len(group) >= need
            assert sizes_agree([b.size_usd for b in group], rules.size_tolerance_pct)
        if out.amendment_id is not None:
            backers = [b for b in ballots if b.action in ("propose", "support")
                       and b.amendment_id == out.amendment_id]
            assert len(backers) >= need and out.amendment_id in amendments
            assert out.size_usd == amendments[out.amendment_id]["size_usd"]
        if out.kind == "pass":
            assert actions.count("pass") >= pass_need
        if out.kind == "send_back":
            assert actions.count("send_back") >= votes_needed(rules.procedural_rule)
        if out.kind == "request_info":
            assert actions.count("request_info") >= (1 if info_rule == "any" else 2)
        if out.kind == "continue":
            # nothing that would have decided the round was ignored
            assert actions.count("pass") < pass_need
            assert actions.count("send_back") < votes_needed(rules.procedural_rule)
        # abstentions never decide anything
        if actions.count("abstain") == 3:
            assert out.kind == "continue"


def test_unanimous_needs_every_member_even_with_abstentions():
    rng = random.Random(7)
    for _ in range(500):
        ballots = [Ballot(member=m, action="invest", argument="x", size_usd=5000)
                   for m in MEMBERS]
        ballots[rng.randrange(3)] = Ballot(member=MEMBERS[rng.randrange(3)], action="abstain",
                                           argument="x")
        assert tally(ballots, BASE).kind != "invest"
