"""What-if replay: re-counting saved ballots under other rules."""

from dataclasses import replace

from qfii.committee.protocol import tally
from qfii.committee.replay import replay_debate, replay_paper
from qfii.config import load_config
from qfii.contracts import Ballot

RULES = replace(load_config().committee, max_rounds=20)
MEMBERS = ("aggressive", "conservative", "neutral")


def rnd(*specs):
    out = []
    for m, spec in zip(MEMBERS, specs):
        action, *rest = spec
        kw = {}
        if action == "invest":
            kw["size_usd"] = rest[0] if rest else 5000
        if action in ("request_info", "send_back"):
            kw.update(target="assessment", request="q")
        if action == "support":
            kw["amendment_id"] = rest[0]
        if action == "propose":
            kw["amendment"] = {"size_usd": rest[0], "summary": "smaller"}
        b = Ballot(member=m, action=action, argument="x", **kw)
        if action == "propose":
            b.amendment_id = rest[1]
        out.append(b)
    return out


def actual(rounds, rules=RULES):
    amendments, kinds = {}, []
    for ballots in rounds:
        for b in ballots:
            if b.action == "propose":
                amendments[b.amendment_id] = b.amendment.model_dump(exclude_none=True)
        kinds.append(tally(ballots, rules, amendments).kind)
    return kinds


# The pattern seen live (MSFT, AMZN, GOOGL): a question, then 2 pass vs 1 invest
# repeated until the round limit.
HOLDOUT = [rnd(("invest",), ("pass",), ("request_info",))] + \
    [rnd(("invest",), ("pass",), ("pass",)) for _ in range(19)]


def test_pass_rule_majority_lets_two_members_close_the_paper():
    ballots = rnd(("invest",), ("pass",), ("pass",))
    assert tally(ballots, RULES).kind == "continue"
    assert tally(ballots, replace(RULES, pass_rule="majority")).kind == "pass"
    # investing still needs everyone
    two_invest = rnd(("invest",), ("invest",), ("pass",))
    assert tally(two_invest, replace(RULES, pass_rule="majority")).kind == "continue"


def test_replay_under_the_rules_it_ran_with_reproduces_the_debate():
    rep = replay_debate(HOLDOUT, actual(HOLDOUT), RULES)
    assert (rep.outcome, rep.rounds, rep.exact) == ("no_consensus", 20, True)


def test_replay_majority_pass_ends_the_holdout_in_round_two():
    rep = replay_debate(HOLDOUT, actual(HOLDOUT), replace(RULES, pass_rule="majority"))
    assert (rep.outcome, rep.rounds, rep.exact) == ("pass", 2, True)


def test_replay_stalemate_stop_matches_the_engine_rule():
    # rounds 2..5 identical: the streak passes 3 in round 5
    rep = replay_debate(HOLDOUT, actual(HOLDOUT),
                        replace(RULES, stop_after_unchanged_rounds=3))
    assert (rep.outcome, rep.rounds, rep.exact) == ("no_consensus", 5, True)


def test_replay_marks_a_diverging_history_inexact():
    # under "majority" a lone question is not answered: the members would have
    # seen no report, so later ballots cannot be trusted
    rep = replay_debate(HOLDOUT, actual(HOLDOUT), replace(RULES, info_request_rule="majority"))
    assert rep.exact is False and rep.outcome == "no_consensus"


def test_replay_keeps_amendment_terms():
    rounds = [rnd(("propose", 3000, "A1"), ("pass",), ("pass",)),
              rnd(("support", "A1"), ("support", "A1"), ("support", "A1"))]
    rep = replay_debate(rounds, actual(rounds), RULES)
    assert (rep.outcome, rep.rounds, rep.size_usd, rep.amendment_id) == ("invest", 2, 3000, "A1")
    # with majority pass the paper would already have closed in round 1
    rep = replay_debate(rounds, actual(rounds), replace(RULES, pass_rule="majority"))
    assert (rep.outcome, rep.rounds) == ("pass", 1)


def test_replay_paper_follows_send_backs_and_sums_rounds():
    back = [rnd(("send_back",), ("send_back",), ("pass",))]
    second = HOLDOUT[:4]
    debates = [(back, actual(back)), (second, actual(second))]
    rep = replay_paper(debates, replace(RULES, pass_rule="majority"))
    assert (rep.outcome, rep.rounds) == ("pass", 1 + 2)


def test_replay_shorter_round_limit_cuts_the_debate():
    rep = replay_debate(HOLDOUT, actual(HOLDOUT), replace(RULES, max_rounds=5))
    assert (rep.outcome, rep.rounds) == ("no_consensus", 5)
