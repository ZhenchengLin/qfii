import json
from datetime import date

import pytest

from qfii.agents.structured import AgentFailed
from qfii.agents.tooluse import research_then_answer
from qfii.contracts import AssessmentAnswer
from qfii.llm import FakeLLM
from qfii.tools.calc import CalcError, calculate
from qfii.tools.client import ToolBox

AS_OF = "2026-10-09"


def test_calculator_is_exact_and_safe():
    assert calculate("229.28 * 24_514_000_000 / 120_067_000_000") == pytest.approx(46.8119, 1e-4)
    assert calculate("max(2, 3) + round(2.5)") == 5
    assert calculate("1,234,567 / 1,000") == pytest.approx(1234.567)
    for bad in ("__import__('os')", "open('x')", "2 ** 1000", "1/0", "a + 1"):
        with pytest.raises(CalcError):
            calculate(bad)


class Script:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen = []

    def __call__(self, system, user):
        self.seen.append(json.loads(user))
        r = self.replies.pop(0)
        return r(self.seen[-1]) if callable(r) else json.dumps(r)


async def test_agent_researches_then_cites_what_it_found(cfg):
    def final(body):
        calc = next(r for r in body["tool_results"] if r["tool"] == "calculate")
        return json.dumps({"final": {"answers": [
            {"claim": f"P/E is {calc['result']['value']}", "evidence_ids": [calc["evidence_id"]]}]}})

    script = Script({"tool_calls": [
        {"tool": "calculate", "arguments": {"expression": "100 * 5 / 25", "label": "P/E"}},
        {"tool": "compute_indicators", "arguments": {"symbol": "msft", "as_of": "2099-01-01"}},
        {"tool": "delete_everything", "arguments": {}}]}, final)
    calls = []
    async with ToolBox(cfg) as tools:
        answer, gathered, trace = await research_then_answer(
            FakeLLM(script), "sys", {"q": "valuation?"}, AssessmentAnswer, tools, AS_OF, set(),
            calls)
    assert answer.answers[0].claim == "P/E is 20.0"
    assert {e.payload["name"] for e in gathered} == {"calculate", "compute_indicators"}
    indicators = next(t for t in trace if t["tool"] == "compute_indicators")
    assert indicators["arguments"] == {"symbol": "MSFT", "as_of": AS_OF}   # cutoff enforced
    errors = [r for r in script.seen[1]["tool_results"] if "error" in r]
    assert errors[0]["tool"] == "delete_everything" and "unknown tool" in errors[0]["error"]
    assert "available_tools" in script.seen[0] and "PROTOCOL" not in script.seen[0]


async def test_invented_citation_is_rejected_then_turns_run_out(cfg):
    bad = {"final": {"answers": [{"claim": "x", "evidence_ids": ["ev_invented"]}]}}
    async with ToolBox(cfg) as tools:
        with pytest.raises(AgentFailed, match="ev_invented"):
            await research_then_answer(FakeLLM(Script(bad, bad)), "s", {}, AssessmentAnswer,
                                       tools, AS_OF, set(), [], max_turns=2)


async def test_teams_report_shows_the_tool_trace(cfg):
    from qfii.agents.research.agent import ResearchAgent
    from qfii.agents.teams.agents import AssessmentAgent
    from qfii.committee.engine import Dossier
    from qfii.committee.runner import Teams
    from qfii.contracts import ResearchRequest
    from qfii.db import connect, init_db
    from qfii.portfolio import snapshot

    conn = connect(":memory:")
    init_db(conn, 100_000)
    packet, _ = await ResearchAgent(cfg).run(ResearchRequest(symbol="NVDA",
                                                             as_of=date(2026, 10, 9)))
    portfolio = snapshot(conn, cfg, date(2026, 10, 9)).model_dump(mode="json")
    assessment = AssessmentAgent(cfg)
    proposal = await assessment.propose(packet, portfolio)
    teams = Teams(cfg, packet, portfolio, assessment, proposal)
    report = await teams.report("assessment", ["what is the valuation?"],
                                Dossier("NVDA", {}, None, portfolio))
    assert report["tool_trace"][0]["tool"] == "calculate"
    assert report["tool_trace"][0]["label"] == "twice the last close"
    assert "Calculated twice the last close" in report["answers"][1]["claim"]


def test_large_results_keep_whole_items_not_cut_text():
    from qfii.agents.tooluse import _trim

    hits = {"hits": [{"rank": i, "text": f"passage {i} " + "x" * 2000} for i in range(12)]}
    out = _trim(hits, 8000)
    assert "truncated" not in out
    assert out["hits"][0]["rank"] == 0                     # best hit kept
    assert all(h["text"].startswith(f"passage {h['rank']} ") for h in out["hits"])
    assert len(json.dumps(out)) <= 8000


async def test_a_failing_team_does_not_end_the_debate(cfg):
    """Live: a RiskReport that never validated crashed the whole debate (ExceptionGroup)."""
    from qfii.agents.research.agent import ResearchAgent
    from qfii.agents.teams.agents import AssessmentAgent
    from qfii.committee.engine import Dossier
    from qfii.committee.runner import Teams
    from qfii.contracts import ResearchRequest
    from qfii.db import connect, init_db
    from qfii.portfolio import snapshot

    conn = connect(":memory:")
    init_db(conn, 100_000)
    packet, _ = await ResearchAgent(cfg).run(ResearchRequest(symbol="NVDA",
                                                             as_of=date(2026, 10, 9)))
    portfolio = snapshot(conn, cfg, date(2026, 10, 9)).model_dump(mode="json")
    assessment = AssessmentAgent(cfg)
    proposal = await assessment.propose(packet, portfolio)
    teams = Teams(cfg, packet, portfolio, assessment, proposal)
    teams.risk.llm.inner._responder = lambda s, u: json.dumps({"final": {"answers": [
        {"question": "q", "evidence_ids": ["ev_nope"]}]}})
    report = await teams.report("risk", ["[neutral] is it liquid?"],
                                Dossier("NVDA", {}, None, portfolio))
    assert report["team"] == "risk" and "no valid RiskReport" in report["error"]


def test_report_field_aliases_seen_live():
    from qfii.contracts import RiskReport

    report = RiskReport.model_validate({
        "symbol": "X", "as_of": AS_OF,
        "answers": [{"question": "[aggressive] q", "answer": "No forward EPS is available.",
                     "data_gap": True}],
        "findings": [{"claim": "Forward valuation is unknown", "severity": "high",
                      "data_gap": True}]})
    assert report.answers[0].claim.startswith("No forward EPS")
    assert report.findings[0].issue == "Forward valuation is unknown"
