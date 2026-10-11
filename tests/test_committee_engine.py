import json
from dataclasses import replace
from datetime import date

import pytest

from qfii.committee.engine import Dossier, run_debate
from qfii.committee.fake import fake_member_responder
from qfii.committee.members import LLMMember, build_members, read_member_prompt
from qfii.committee.memory import recall, recall_all, remember
from qfii.committee.runner import run_paper
from qfii.config import COMMITTEE, load_config
from qfii.contracts import Ballot
from qfii.db import connect, init_db
from qfii.llm import FakeLLM
from qfii.portfolio import snapshot

RULES = replace(load_config().committee, max_rounds=3)  # scripted 3-round cases
AS_OF = date(2026, 10, 9)


class Scripted:
    """Member that plays a fixed ballot per round and records what it saw."""

    def __init__(self, name, *ballots):
        self.name = name
        self.ballots = list(ballots)
        self.views = []

    async def deliberate(self, view):
        self.views.append(view)
        spec = self.ballots.pop(0)
        if isinstance(spec, Exception):
            raise spec
        return Ballot(member=self.name, argument=f"{self.name} r{view.round}", **spec)


class Teams:
    def __init__(self):
        self.asked = []

    async def report(self, team, requests, dossier):
        self.asked.append((team, requests))
        return {"team": team, "answer": f"{team} answer"}


def dossier():
    return Dossier("AAPL", {"thesis": "t"}, {"risks": []},
                   {"cash_usd": 100000, "equity_usd": 100000, "holdings": [],
                    "limits": {"max_order_usd": 10000, "max_position_pct": 10,
                               "min_cash_pct": 20}})


INV = {"action": "invest", "size_usd": 5000}
PASS = {"action": "pass"}
ASK = {"action": "request_info", "target": "risk", "request": "downside?"}


async def test_info_request_report_reaches_every_member_next_round():
    a = Scripted("aggressive", INV, INV)
    c = Scripted("conservative", ASK, INV)
    n = Scripted("neutral", ASK, INV)
    teams = Teams()
    result = await run_debate(dossier(), [a, c, n], teams, RULES)
    assert result.outcome == "invest" and result.size_usd == 5000
    assert len(result.rounds) == 2
    assert teams.asked == [("risk", ["[conservative] downside?", "[neutral] downside?"])]
    for m in (a, c, n):
        assert m.views[0].info_reports == []
        assert m.views[1].info_reports[0]["report"]["answer"] == "risk answer"
        assert m.views[1].transcript[0]["result"] == "request_info"


async def test_no_consensus_after_three_rounds_shelves():
    a = Scripted("aggressive", INV, INV, INV)
    c = Scripted("conservative", PASS, PASS, PASS)
    n = Scripted("neutral", INV, INV, INV)
    result = await run_debate(dossier(), [a, c, n], Teams(), RULES)
    assert result.outcome == "no_consensus" and len(result.rounds) == 3
    assert result.final is None


async def test_question_in_the_last_round_is_still_answered():
    teams = Teams()
    members = [Scripted(m, PASS, INV, ASK) for m in ("aggressive", "conservative")]
    members.append(Scripted("neutral", INV, PASS, ASK))
    result = await run_debate(dossier(), members, teams, RULES)
    assert result.outcome == "no_consensus"
    assert teams.asked and result.rounds[-1].reports[0]["team"] == "risk"


async def test_send_back_ends_debate():
    back = {"action": "send_back", "target": "assessment", "request": "redo"}
    members = [Scripted("aggressive", back), Scripted("conservative", back),
               Scripted("neutral", PASS)]
    result = await run_debate(dossier(), members, Teams(), RULES)
    assert result.outcome == "send_back" and result.final.teams == ["assessment"]


async def test_failing_member_abstains_and_cannot_decide_anything():
    members = [Scripted("aggressive", INV, INV, INV), Scripted("neutral", INV, INV, INV),
               Scripted("conservative", ValueError("bad json"), PASS, PASS)]
    result = await run_debate(dossier(), members, Teams(), RULES)
    assert result.outcome == "no_consensus"
    assert result.rounds[0].ballots[2].action == "abstain"
    assert result.rounds[0].ballots[2].argument.startswith("[no valid ballot")


async def test_three_failures_do_not_produce_a_pass():
    """Live run, 2026-10-10: three broken ballots used to count as a unanimous pass."""
    members = [Scripted(m, *[RuntimeError("timeout")] * 3) for m in COMMITTEE]
    result = await run_debate(dossier(), members, Teams(), RULES)
    assert result.outcome == "no_consensus"
    assert all(b.action == "abstain" for r in result.rounds for b in r.ballots)


def test_zero_size_on_non_trade_ballot_is_accepted():
    """Live run: the model filled size_usd = 0 on a pass and the ballot was rejected."""
    b = Ballot(member="neutral", action="pass", argument="x", size_usd=0)
    assert b.size_usd is None
    with pytest.raises(ValueError):
        Ballot(member="neutral", action="invest", argument="x", size_usd=0)


async def test_members_see_only_their_own_memory():
    members = [Scripted(m, PASS) for m in COMMITTEE]
    memories = {m: [f"{m} secret"] for m in COMMITTEE}
    await run_debate(dossier(), members, Teams(), RULES, memories)
    for m in members:
        assert m.views[0].memory == [f"{m.name} secret"]


async def test_committee_must_be_exactly_three_members():
    with pytest.raises(ValueError, match="exactly"):
        await run_debate(dossier(), [Scripted("aggressive")], Teams(), RULES)


async def test_llm_member_cannot_sign_as_someone_else():
    llm = FakeLLM(lambda s, u: json.dumps({"member": "aggressive", "action": "pass",
                                           "argument": "no"}))
    member = LLMMember("conservative", llm)
    result = await run_debate(dossier(), [member, Scripted("aggressive", PASS),
                                          Scripted("neutral", PASS)], Teams(), RULES)
    assert result.rounds[0].ballots[0].member == "conservative"
    system, user = llm.prompts[0]
    assert "Ballot actions" in system and "CONSERVATIVE member" in user
    assert read_member_prompt(user)["you_are"] == "conservative"


async def test_members_share_a_prompt_prefix_and_retry_with_feedback():
    replies = iter(["not json", json.dumps({"action": "pass", "argument": "ok"})])
    llms = {m: FakeLLM(lambda s, u, m=m: next(replies) if m == "neutral"
                       else json.dumps({"action": "pass", "argument": "x"})) for m in COMMITTEE}
    members = [LLMMember(m, llms[m]) for m in COMMITTEE]
    result = await run_debate(dossier(), members, Teams(), RULES)
    assert result.outcome == "pass"
    prefixes = {llms[m].prompts[0][1].split("## You")[0] for m in COMMITTEE}
    assert len(prefixes) == 1  # identical shared part -> prompt cache hit
    assert "your_previous_reply_was_rejected_because" in llms["neutral"].prompts[1][1]
    assert [c["ok"] for c in members[2].calls] == [False, True]


def test_memory_isolation_in_db():
    conn = connect(":memory:")
    init_db(conn, 1000)
    remember(conn, "aggressive", "lesson", "growth wins")
    remember(conn, "conservative", "lesson", "protect capital")
    assert recall(conn, "aggressive", 10) == ["[lesson] growth wins"]
    assert recall_all(conn, 10)["neutral"] == []
    with pytest.raises(ValueError):
        remember(conn, "chair", "lesson", "x")


async def test_fake_committee_negotiates_to_one_size(cfg):
    conn = connect(cfg.db_path)
    init_db(conn, cfg.limits.starting_cash_usd)
    snap = snapshot(conn, cfg, AS_OF).model_dump(mode="json")
    d = Dossier("NVDA", {}, {}, snap)
    result = await run_debate(d, build_members(cfg), Teams(), cfg.committee)
    assert [r.outcome.kind for r in result.rounds] == ["request_info", "continue", "invest"]
    assert result.size_usd == 6000  # 0.6 x min($10k order, 10% of $100k equity)


async def test_run_paper_saves_everything(cfg):
    conn = connect(cfg.db_path)
    init_db(conn, cfg.limits.starting_cash_usd)
    run = await run_paper(cfg, conn, "NVDA", AS_OF, save=True)
    assert run.debate.outcome == "invest"
    q = lambda sql: conn.execute(sql).fetchall()
    assert q("SELECT state FROM papers")[0][0] == "decided"
    assert len(q("SELECT * FROM ballots")) == 9
    assert len(q("SELECT * FROM info_requests")) == 1
    assert tuple(q("SELECT decision, size_usd FROM decisions")[0]) == ("invest", 6000)
    members = {r[0] for r in q("SELECT DISTINCT agent FROM llm_calls")}
    assert members == {"research", "assessment", "risk", *COMMITTEE}
    proposal = json.loads(q("SELECT body_json FROM proposals")[0][0])
    assert proposal["verdict"] == "submit" and proposal["arguments"][0]["evidence_ids"]
    report = json.loads(q("SELECT report_json FROM info_requests")[0][0])
    assert report["team"] == "risk" and report["findings"][0]["severity"] == "medium"
    assert tuple(q("SELECT agent, tier FROM llm_calls WHERE agent='neutral'")[0]) \
        == ("neutral", "strongest")
    # each member got its own reflection plus its own lesson
    assert recall(conn, "neutral", 10)[0].startswith("[reflection] NVDA: committee outcome invest")
    assert len(recall(conn, "aggressive", 10)) == 2
    # second paper: members now see their own memory
    run2 = await run_paper(cfg, conn, "AAPL", AS_OF, save=True)
    assert run2.debate.rounds[0].ballots  # ran
    assert q("SELECT COUNT(*) FROM debates")[0][0] == 2


def test_fake_responder_passes_when_no_room():
    view = {"round": 1, "final_round": False, "symbol": "X", "additional_reports": [],
            "previous_rounds": [],
            "portfolio": {"cash_usd": 10000, "equity_usd": 100000, "holdings": [],
                          "limits": {"max_order_usd": 10000, "max_position_pct": 10,
                                     "min_cash_pct": 20}}}
    user = json.dumps(view) + "\n\n## You\npersona\n" + json.dumps({"you_are": "aggressive",
                                                                  "your_memory": []})
    reply = json.loads(fake_member_responder("aggressive")("", user))
    assert reply["action"] == "pass"


async def test_risk_is_not_commissioned_until_the_committee_asks():
    d = Dossier("AAPL", {"thesis": "t"}, None, dossier().portfolio)
    members = [Scripted("aggressive", INV, INV), Scripted("conservative", ASK, INV),
               Scripted("neutral", ASK, INV)]
    teams = Teams()
    result = await run_debate(d, members, teams, RULES)
    first = members[0].views[0].as_prompt_dict()
    assert first["risk_report"].startswith("not commissioned")
    assert teams.asked[0][0] == "risk"
    assert members[0].views[1].info_reports[0]["team"] == "risk"
    assert result.outcome == "invest"


async def test_size_above_hard_limits_is_rejected_and_retried():
    """Live run: R1 14B proposed $15,000 when the limits allowed $10,000."""
    replies = iter([json.dumps({"action": "invest", "size_usd": 15000, "argument": "big"}),
                    json.dumps({"action": "invest", "size_usd": 9000, "argument": "ok"})])
    llm = FakeLLM(lambda s, u: next(replies))
    member = LLMMember("neutral", llm)
    nine = {"action": "invest", "size_usd": 9000}
    others = [Scripted("aggressive", nine), Scripted("conservative", nine)]
    result = await run_debate(dossier(), [member, *others], Teams(), RULES)
    assert '"largest_allowed_order_usd":10000' in llm.prompts[0][1]
    assert "exceeds largest_allowed_order_usd 10,000" in llm.prompts[1][1]
    assert [c["ok"] for c in member.calls] == [False, True]
    assert (result.outcome, result.size_usd) == ("invest", 9000)


def test_max_new_order():
    from qfii.portfolio import max_new_order_usd
    snap = {"cash_usd": 25000, "equity_usd": 100000,
            "limits": {"max_order_usd": 10000, "max_position_pct": 10, "min_cash_pct": 20}}
    assert max_new_order_usd(snap) == 5000  # only $5k above the 20% cash floor


async def test_members_are_not_told_the_round_limit():
    members = [Scripted(m, PASS) for m in COMMITTEE]
    await run_debate(dossier(), members, Teams(), RULES)
    shown = members[0].views[0].as_prompt_dict()
    assert not {"round", "max_rounds", "final_round"} & set(shown)
    assert "max_rounds" not in shown["rules"]


async def test_changing_a_vote_requires_a_reason():
    """Anti-herding: a member that flips must name what changed its mind."""
    replies = iter([
        json.dumps({"action": "invest", "size_usd": 9000, "argument": "upside"}),
        json.dumps({"action": "pass", "argument": "fine, whatever"}),          # flip, no reason
        json.dumps({"action": "pass", "argument": "ok", "changed_because": "risk report "
                    "showed a 34% drawdown"}),
    ])
    llm = FakeLLM(lambda s, u: next(replies))
    member = LLMMember("aggressive", llm)
    others = [Scripted("conservative", PASS, PASS), Scripted("neutral", PASS, PASS)]
    result = await run_debate(dossier(), [member, *others], Teams(), RULES)
    assert "fill changed_because" in llm.prompts[2][1]
    final = result.rounds[1].ballots[0]
    assert final.action == "pass" and final.changed_because.startswith("risk report")
    assert result.outcome == "pass"


async def test_default_committee_debates_up_to_twenty_rounds(cfg):
    from qfii.papers import create_paper, transition
    from qfii.store import save_debate

    rules = load_config().committee
    assert rules.max_rounds == 20
    members = [Scripted("aggressive", *[INV] * 20), Scripted("conservative", *[PASS] * 20),
               Scripted("neutral", *[INV] * 20)]
    result = await run_debate(dossier(), members, Teams(), rules)
    assert result.outcome == "no_consensus" and len(result.rounds) == 20
    conn = connect(cfg.db_path)            # rounds 11-20 must fit the ballots table too
    init_db(conn, cfg.limits.starting_cash_usd)
    with conn:
        pid = create_paper(conn, "AAPL", "user", "research")
        transition(conn, pid, "packet_ready", "research")
        transition(conn, pid, "proposal", "assessment")
    save_debate(conn, pid, "AAPL", result)
    assert conn.execute("SELECT MAX(round) FROM ballots").fetchone()[0] == 20


PROPOSE = {"action": "propose", "amendment": {"size_usd": 4000, "summary": "half size, 15% stop",
                                              "stop_loss_pct": 15}}
SUPPORT_A1 = {"action": "support", "amendment_id": "A1"}


async def test_members_negotiate_through_an_amendment():
    c = Scripted("conservative", PROPOSE, SUPPORT_A1)
    a = Scripted("aggressive", INV, {**SUPPORT_A1, "changed_because": "a stop answers my risk"})
    n = Scripted("neutral", PASS, SUPPORT_A1)
    result = await run_debate(dossier(), [a, c, n], Teams(), RULES)
    assert result.outcome == "invest" and result.final.amendment_id == "A1"
    assert result.size_usd == 4000 and result.final.amendment["stop_loss_pct"] == 15
    table = a.views[1].as_prompt_dict()["amendments_on_the_table"]
    assert table[0]["id"] == "A1" and table[0]["proposed_by"] == "conservative"
    assert a.views[0].as_prompt_dict()["amendments_on_the_table"] == []


async def test_llm_member_cannot_back_a_missing_amendment_or_oversize():
    replies = iter([json.dumps({"action": "support", "amendment_id": "A9", "argument": "x"}),
                    json.dumps({"action": "propose", "argument": "big",
                                "amendment": {"size_usd": 50000, "summary": "go big"}})])
    fallback = json.dumps({"action": "pass", "argument": "pass"})
    llm = FakeLLM(lambda s, u: next(replies, fallback))
    member = LLMMember("aggressive", llm)
    others = [Scripted("conservative", *[PASS] * 3), Scripted("neutral", *[PASS] * 3)]
    result = await run_debate(dossier(), [member, *others], Teams(), RULES)
    assert "A9 is not on the table" in llm.prompts[1][1]
    assert result.rounds[0].ballots[0].action == "abstain"   # oversize retry failed too


async def test_amendment_decision_is_saved_and_shown(cfg):
    from qfii.store import save_debate
    from qfii.web.app import build_app

    conn = connect(cfg.db_path)
    init_db(conn, cfg.limits.starting_cash_usd)
    from qfii.papers import create_paper, transition
    with conn:
        pid = create_paper(conn, "AAPL", "user", "research")
        transition(conn, pid, "packet_ready", "research")
        transition(conn, pid, "proposal", "assessment")
    members = [Scripted("conservative", PROPOSE, SUPPORT_A1),
               Scripted("aggressive", INV, {**SUPPORT_A1, "changed_because": "ok"}),
               Scripted("neutral", PASS, SUPPORT_A1)]
    result = await run_debate(dossier(), members, Teams(), RULES)
    did = save_debate(conn, pid, "AAPL", result)
    row = conn.execute("SELECT amendment_id, terms_json FROM decisions").fetchone()
    assert row[0] == "A1" and json.loads(row[1])["stop_loss_pct"] == 15
    detail = build_app(cfg).state.server.debate(did)
    assert detail["decision_terms"]["amendment_id"] == "A1"
    assert detail["amendments"][0]["proposed_by"] == "conservative"
    aggressive_r2 = next(b for b in detail["rounds"][1]["ballots"] if b["member"] == "aggressive")
    assert aggressive_r2["changed_because"] == "ok"   # the reason survives in history
    assert [r["result"] for r in detail["rounds"]] == ["continue", "invest"]


async def test_members_see_how_long_positions_have_been_unchanged():
    members = [Scripted("aggressive", *[INV] * 3), Scripted("conservative", *[PASS] * 3),
               Scripted("neutral", *[PASS] * 3)]
    await run_debate(dossier(), members, Teams(), RULES)
    streaks = [v.as_prompt_dict()["rounds_with_unchanged_positions"] for v in members[0].views]
    assert streaks == [0, 1, 2]
