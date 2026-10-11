import json
from dataclasses import replace
from datetime import date

import pytest

from qfii.agents.structured import AgentFailed
from qfii.agents.teams.agents import AssessmentAgent, RiskAgent
from qfii.committee import runner
from qfii.committee.runner import run_paper
from qfii.db import connect, init_db
from qfii.papers import history
from qfii.portfolio import snapshot

AS_OF = date(2026, 10, 9)


@pytest.fixture
def conn(cfg):
    c = connect(cfg.db_path)
    init_db(c, cfg.limits.starting_cash_usd)
    return c


async def packet_and_portfolio(cfg, conn):
    from qfii.agents.research.agent import ResearchAgent
    from qfii.contracts import ResearchRequest

    packet, _ = await ResearchAgent(cfg).run(ResearchRequest(symbol="NVDA", as_of=AS_OF))
    return packet, snapshot(conn, cfg, AS_OF).model_dump(mode="json")


async def test_assessment_rejects_invented_citations(cfg, conn):
    packet, portfolio = await packet_and_portfolio(cfg, conn)
    agent = AssessmentAgent(cfg)
    good = json.loads(agent.llm.inner._responder("", json.dumps(
        agent._base(packet, portfolio), default=str)))
    bad = {**good, "arguments": [{"claim": "made up", "evidence_ids": ["ev_nope"]}]}
    agent.llm.inner._responder = lambda s, u: json.dumps(bad)
    with pytest.raises(AgentFailed, match="ev_nope") as info:
        await agent.propose(packet, portfolio)
    assert len(info.value.calls) == 2


async def test_risk_adds_portfolio_impact_facts(cfg, conn):
    packet, portfolio = await packet_and_portfolio(cfg, conn)
    proposal = await AssessmentAgent(cfg).propose(packet, portfolio)
    risk = RiskAgent(cfg)
    report = await risk.review(packet, portfolio, proposal, ["is it liquid?"])
    impact = {e.payload["name"]: e.payload["value"] for e in risk.evidence
              if e.source == "code:portfolio_impact"}
    assert impact["proposed_order_usd"] == 5000  # 5% of $100k, under the $10k cap
    assert impact["cash_after_usd"] == 95000
    assert impact["est_round_trip_cost_usd"] == 5.0  # 2 x 5 bps x $5k
    assert report.findings and report.answers


async def test_assessment_reject_stops_before_the_committee(cfg, conn, monkeypatch):
    def reject(system, user):
        data = json.loads(user)
        f = data["facts"][0]
        return json.dumps({"thesis": "no", "arguments": [{"claim": "x", "evidence_ids": [f["id"]]}],
                           "counterarguments": [{"claim": "y", "evidence_ids": [f["id"]]}],
                           "feasibility": "n/a", "invalidation_conditions": ["z"],
                           "expected_holding_period_days": 30, "suggested_position_pct": 0,
                           "verdict": "reject"})

    monkeypatch.setattr("qfii.agents.teams.agents.fake_assessment_responder", reject)
    run = await run_paper(cfg, conn, "NVDA", AS_OF, save=True)
    assert run.outcome == "assessment_reject" and run.debates == []
    assert [h[1] for h in history(conn, run.paper_id)][-1] == "rejected"


async def test_send_back_revises_and_debates_again(cfg, conn, monkeypatch):
    from qfii.committee.engine import run_debate as real_run_debate

    calls = {"n": 0}

    async def debate(dossier, members, teams, rules, memories=None, on_round=None,
                     on_ballot=None):
        calls["n"] += 1
        if calls["n"] == 1:  # first debate: everyone sends it back to Assessment
            from qfii.committee.engine import DebateResult, RoundRecord
            from qfii.committee.protocol import RoundOutcome
            from qfii.contracts import Ballot
            ballots = [Ballot(member=m, action="send_back", target="assessment",
                              request="justify the size", argument="weak")
                       for m in ("aggressive", "conservative", "neutral")]
            out = RoundOutcome("send_back", {"send_back": 3}, teams=["assessment"],
                               requests=["[neutral] justify the size"])
            return DebateResult("send_back", [RoundRecord(1, ballots, out)], out)
        return await real_run_debate(dossier, members, teams, rules, memories, on_round,
                                     on_ballot)

    monkeypatch.setattr(runner, "run_debate", debate)
    proposals = []
    run = await run_paper(cfg, conn, "NVDA", AS_OF, save=True,
                          on_proposal=lambda p, v: proposals.append(v))
    assert proposals == [1, 2] and len(run.debates) == 2
    assert run.outcome == "invest"
    states = [h[1] for h in history(conn, run.paper_id)]
    assert states[-4:] == ["proposal", "sent_back", "proposal", "decided"]
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 2


async def test_send_back_limit_shelves(cfg, conn, monkeypatch):
    from qfii.committee.engine import DebateResult, RoundRecord
    from qfii.committee.protocol import RoundOutcome

    async def always_back(dossier, members, teams, rules, memories=None, on_round=None,
                          on_ballot=None):
        out = RoundOutcome("send_back", {"send_back": 3}, teams=["assessment"], requests=["x"])
        return DebateResult("send_back", [RoundRecord(1, [], out)], out)

    monkeypatch.setattr(runner, "run_debate", always_back)
    run = await run_paper(replace(cfg, committee=replace(cfg.committee, max_send_backs=1)),
                          conn, "NVDA", AS_OF, save=False)
    assert run.outcome == "shelved_after_send_back" and len(run.debates) == 2


async def test_needs_evidence_queues_research_requests(cfg, conn, monkeypatch):
    def needs(system, user):
        data = json.loads(user)
        assert data["research_can_fetch"]["market_news_headlines"] is False
        f = data["facts"][0]
        return json.dumps({"thesis": "?", "arguments": [{"claim": "x", "evidence_ids": [f["id"]]}],
                           "counterarguments": [{"claim": "y", "evidence_ids": [f["id"]]}],
                           "feasibility": "n/a", "invalidation_conditions": ["z"],
                           "expected_holding_period_days": 30, "suggested_position_pct": 1,
                           "verdict": "needs_evidence",
                           "research_questions": ["latest 10-year yield trend?"]})

    monkeypatch.setattr("qfii.agents.teams.agents.fake_assessment_responder", needs)
    run = await run_paper(cfg, conn, "NVDA", AS_OF, save=True)
    assert run.outcome == "assessment_needs_evidence"
    row = conn.execute("SELECT paper_id, question, requested_by FROM research_requests").fetchone()
    assert tuple(row) == (run.paper_id, "latest 10-year yield trend?", "assessment")


def test_fetchable_sources_reflect_config(cfg, tmp_path):
    from qfii.agents.teams.agents import fetchable_sources

    assert fetchable_sources(cfg, "NVDA")["fundamentals"] is False
    assert fetchable_sources(replace(cfg, sec_user_agent="Qfii x@y.z"), "NVDA")["fundamentals"]
