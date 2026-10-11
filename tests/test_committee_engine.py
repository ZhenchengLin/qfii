import json
from datetime import date

import pytest

from qfii.committee.engine import Dossier, run_debate
from qfii.committee.fake import fake_member_responder
from qfii.committee.members import LLMMember, build_members
from qfii.committee.memory import recall, recall_all, remember
from qfii.committee.runner import run_paper
from qfii.config import COMMITTEE, load_config
from qfii.contracts import Ballot
from qfii.db import connect, init_db
from qfii.llm import FakeLLM
from qfii.portfolio import snapshot

RULES = load_config().committee
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


async def test_info_request_on_final_round_is_not_fetched():
    teams = Teams()
    members = [Scripted(m, PASS, INV, ASK) for m in ("aggressive", "conservative")]
    members.append(Scripted("neutral", INV, PASS, ASK))
    result = await run_debate(dossier(), members, teams, RULES)
    assert result.outcome == "no_consensus" and teams.asked == []


async def test_send_back_ends_debate():
    back = {"action": "send_back", "target": "assessment", "request": "redo"}
    members = [Scripted("aggressive", back), Scripted("conservative", back),
               Scripted("neutral", PASS)]
    result = await run_debate(dossier(), members, Teams(), RULES)
    assert result.outcome == "send_back" and result.final.teams == ["assessment"]


async def test_failing_member_counts_as_pass_and_cannot_force_trade():
    members = [Scripted("aggressive", INV, INV, INV), Scripted("neutral", INV, INV, INV),
               Scripted("conservative", ValueError("bad json"), PASS, PASS)]
    result = await run_debate(dossier(), members, Teams(), RULES)
    assert result.outcome == "no_consensus"
    assert result.rounds[0].ballots[2].argument.startswith("[invalid ballot")


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
    assert "CONSERVATIVE member" in system and "Ballot actions" in system
    assert json.loads(user)["you_are"] == "conservative"


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
    assert members == {"research", *COMMITTEE}
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
    reply = json.loads(fake_member_responder("aggressive")("", json.dumps(view)))
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
