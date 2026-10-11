from dataclasses import replace

import pytest

from qfii.committee.protocol import sizes_agree, tally, votes_needed
from qfii.config import load_config
from qfii.contracts import Ballot

RULES = replace(load_config().committee, max_rounds=3)  # scripted 3-round cases  # unanimous trades, majority procedural, 10% size band
MAJORITY = replace(RULES, invest_rule="majority")


def b(member, action, size=None, sym=None, target=None, request=None):
    return Ballot(member=member, action=action, argument="x", size_usd=size,
                  replace_symbol=sym, target=target, request=request)


def three(*specs):
    return [b(m, *s) for m, s in zip(("aggressive", "conservative", "neutral"), specs)]


def test_votes_needed():
    assert votes_needed("unanimous") == 3 and votes_needed("majority") == 2


def test_sizes_agree_within_band():
    assert sizes_agree([5000, 5200, 5400], 10)       # 400 / 5200 = 7.7%
    assert not sizes_agree([5000, 6000, 7500], 10)
    assert sizes_agree([5000], 0)


def test_unanimous_invest_uses_median_size():
    out = tally(three(("invest", 5000), ("invest", 5200), ("invest", 5400)), RULES)
    assert (out.kind, out.size_usd) == ("invest", 5200)


def test_size_disagreement_is_not_consensus():
    out = tally(three(("invest", 5000), ("invest", 6000), ("invest", 7500)), RULES)
    assert out.kind == "continue" and "sizes differ" in out.detail


def test_two_of_three_is_not_enough_to_trade_under_unanimity():
    out = tally(three(("invest", 5000), ("invest", 5000), ("pass",)), RULES)
    assert out.kind == "continue"
    assert tally(three(("invest", 5000), ("invest", 5000), ("pass",)), MAJORITY).kind == "invest"


def test_unanimous_pass():
    assert tally(three(("pass",), ("pass",), ("pass",)), RULES).kind == "pass"


def test_replace_needs_same_position_to_sell():
    same = three(("replace", 5000, "MSFT"), ("replace", 5000, "msft"), ("replace", 5100, "MSFT"))
    out = tally(same, RULES)
    assert (out.kind, out.replace_symbol, out.size_usd) == ("replace", "MSFT", 5000)
    diff = three(("replace", 5000, "MSFT"), ("replace", 5000, "JPM"), ("replace", 5000, "MSFT"))
    assert "which position to sell" in tally(diff, RULES).detail


def test_majority_requests_info_from_union_of_teams():
    out = tally(three(("invest", 9000), ("request_info", None, None, "risk", "drawdown?"),
                      ("request_info", None, None, "assessment", "moat?")), RULES)
    assert out.kind == "request_info"
    assert out.teams == ["assessment", "risk"]
    assert out.requests == ["[conservative] drawdown?", "[neutral] moat?"]


def test_both_expands_to_both_teams():
    out = tally(three(("request_info", None, None, "both", "q"), ("pass",),
                      ("request_info", None, None, "risk", "r")), RULES)
    assert out.teams == ["assessment", "risk"]


def test_send_back_by_majority():
    out = tally(three(("send_back", None, None, "assessment", "thesis ignores debt"),
                      ("send_back", None, None, "assessment", "redo valuation"), ("pass",)), RULES)
    assert out.kind == "send_back" and out.teams == ["assessment"]


def test_single_procedural_vote_does_nothing():
    out = tally(three(("invest", 9000), ("request_info", None, None, "risk", "q"), ("pass",)),
                RULES)
    assert out.kind == "continue"


@pytest.mark.parametrize("kwargs", [
    {"action": "invest"},
    {"action": "replace", "size_usd": 10},
    {"action": "request_info", "request": "q"},
    {"action": "send_back", "target": "risk"},
])
def test_invalid_ballots_rejected(kwargs):
    with pytest.raises(ValueError):
        Ballot(member="neutral", argument="x", **kwargs)


def test_irrelevant_fields_dropped():
    ballot = Ballot(member="neutral", action="pass", argument="x", size_usd=5, target="risk",
                    request="q")
    assert (ballot.size_usd, ballot.target, ballot.request) == (None, None, None)


@pytest.mark.parametrize("reply", [
    # observed from DeepSeek-R1 14B on 2026-10-10: every field filled in
    {"action": "pass", "argument": "too risky", "size_usd": 0, "replace_symbol": "",
     "target": "", "request": "", "note_to_self": ""},
    {"action": "invest", "argument": "upside", "size_usd": 10000, "replace_symbol": "",
     "target": "none", "request": ""},
    {"action": "request_info", "argument": "need data", "size_usd": 0, "replace_symbol": "",
     "target": "assessment", "request": "fundamentals?"},
])
def test_filled_in_unused_fields_are_dropped(reply):
    ballot = Ballot(member="neutral", **reply)
    assert ballot.action == reply["action"]
    if ballot.action != "request_info":
        assert ballot.target is None and ballot.request is None
    assert ballot.replace_symbol is None and ballot.note_to_self is None


def test_data_gap_claims_need_no_citation():
    from qfii.contracts import Claim, RiskFinding, RiskReport
    gap = Claim(claim="No fundamentals are available.", data_gap=True)
    assert gap.evidence_ids == []
    with pytest.raises(ValueError, match="data_gap"):
        Claim(claim="Revenue grew.")
    report = RiskReport(symbol="X", as_of="2026-10-09", findings=[
        RiskFinding(issue="No news coverage", severity="medium", data_gap=True)])
    assert report.findings[0].data_gap


TERMS = {"size_usd": 4000.0, "summary": "smaller with a stop", "stop_loss_pct": 15.0,
         "proposed_by": "conservative", "proposed_in_round": 1}


def amend(member, action, aid, size=4000.0):
    extra = {"amendment": {"size_usd": size, "summary": "smaller with a stop"}} \
        if action == "propose" else {}
    ballot = Ballot(member=member, action=action, argument="x", amendment_id=aid, **extra)
    ballot.amendment_id = aid
    return ballot


def test_unanimous_backing_of_an_amendment_is_a_decision_on_its_terms():
    ballots = [amend("aggressive", "support", "A1"), amend("conservative", "support", "A1"),
               amend("neutral", "support", "A1")]
    out = tally(ballots, RULES, {"A1": TERMS})
    assert (out.kind, out.amendment_id, out.size_usd) == ("invest", "A1", 4000.0)
    assert out.amendment["stop_loss_pct"] == 15.0


def test_split_amendment_backing_continues():
    ballots = [amend("aggressive", "propose", "A2", 8000), amend("conservative", "support", "A1"),
               amend("neutral", "support", "A1")]
    out = tally(ballots, RULES, {"A1": TERMS, "A2": {**TERMS, "size_usd": 8000}})
    assert out.kind == "continue" and "A1 by conservative/neutral" in out.detail
    assert tally(ballots, MAJORITY, {"A1": TERMS, "A2": TERMS}).amendment_id == "A1"


def test_ballot_needs_terms_or_id():
    with pytest.raises(ValueError, match="propose needs an amendment"):
        Ballot(member="neutral", action="propose", argument="x")
    with pytest.raises(ValueError, match="support needs amendment_id"):
        Ballot(member="neutral", action="support", argument="x")
