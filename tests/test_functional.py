"""Functional tests: whole flows end to end (research -> Assessment -> committee ->
store -> UI/CLI), with scripted models so they run offline and deterministically."""

import json
from datetime import date
from pathlib import Path

import pytest

from qfii import cli
from qfii.committee import fake as committee_fake
from qfii.committee.members import read_member_prompt
from qfii.committee.runner import run_paper
from qfii.db import connect, init_db
from qfii.evaluate import evaluate, summarize
from qfii.papers import history

AS_OF = date(2026, 10, 9)


@pytest.fixture
def conn(cfg):
    c = connect(cfg.db_path)
    init_db(c, cfg.limits.starting_cash_usd)
    return c


def scripted_committee(monkeypatch, plan):
    """plan[member] = list of ballot dicts by round (the last repeats)."""
    def factory(member):
        def respond(system, user):
            view = read_member_prompt(user)
            r = len(view["previous_rounds"])
            ballots = plan[member]
            return json.dumps({"argument": f"{member} round {r + 1}",
                               **ballots[min(r, len(ballots) - 1)]})
        return respond
    monkeypatch.setattr(committee_fake, "fake_member_responder", factory)
    monkeypatch.setattr("qfii.committee.members.fake_member_responder", factory)


# ------------------------------------------------------------------ evaluation

async def test_eval_measures_rounds_to_conclusion(cfg, tmp_path):
    result = await evaluate(cfg, ["AAPL", "MSFT", "!!"], AS_OF, tmp_path)
    s = result["summary"]
    assert (s["papers"], s["errors"], s["concluded"]) == (3, 1, 2)
    assert s["avg_rounds_to_conclusion"] == 3 and s["conclusion_rate"] == 1.0
    assert s["info_requests"] == 2 and s["report_failures"] == 0
    run_dir = Path(result["dir"])
    assert len((run_dir / "papers.jsonl").read_text().splitlines()) == 3
    assert "Average rounds to conclusion: 3" in (run_dir / "report.md").read_text()


def test_summary_separates_no_consensus_from_concluded():
    rows = [
        {"symbol": "A", "outcome": "invest", "concluded": True, "rounds_to_conclusion": 2,
         "rounds_total": 2, "debates": 1, "decided_on_amendment": True, "info_requests": 1,
         "report_failures": 0, "amendments": 1, "vote_changes": 1, "abstentions": 0,
         "seconds": 10, "error": None},
        {"symbol": "B", "outcome": "no_consensus", "concluded": False,
         "rounds_to_conclusion": None, "rounds_total": 20, "debates": 1,
         "decided_on_amendment": False, "info_requests": 2, "report_failures": 1,
         "amendments": 0, "vote_changes": 0, "abstentions": 1, "seconds": 30, "error": None},
        {"symbol": "C", "outcome": "assessment_reject", "concluded": False,
         "rounds_to_conclusion": None, "rounds_total": 0, "debates": 0,
         "decided_on_amendment": False, "info_requests": 0, "report_failures": 0,
         "amendments": 0, "vote_changes": 0, "abstentions": 0, "seconds": 5, "error": None},
    ]
    s = summarize(rows, 20)
    assert s["reached_committee"] == 2 and s["concluded"] == 1 and s["conclusion_rate"] == 0.5
    assert s["avg_rounds_to_conclusion"] == 2 and s["avg_rounds_all_debated"] == 11
    assert s["outcomes"] == {"invest": 1, "no_consensus": 1, "assessment_reject": 1}


# ------------------------------------------------------------------ negotiation

async def test_committee_agrees_on_an_amendment_end_to_end(cfg, conn, monkeypatch):
    amend = {"action": "propose", "amendment": {"size_usd": 4000, "summary": "half size",
                                                "stop_loss_pct": 15}}
    support = {"action": "support", "amendment_id": "A1"}
    scripted_committee(monkeypatch, {
        "conservative": [amend, support],
        "aggressive": [{"action": "invest", "size_usd": 9000},
                       {**support, "changed_because": "the stop answers my concern"}],
        "neutral": [{"action": "pass"}, {**support, "changed_because": "a smaller size fits"}],
    })
    run = await run_paper(cfg, conn, "NVDA", AS_OF, save=True)
    assert run.outcome == "invest" and run.debate.final.amendment_id == "A1"
    row = conn.execute("SELECT decision, size_usd, amendment_id, terms_json FROM decisions"
                       ).fetchone()
    assert tuple(row)[:3] == ("invest", 4000, "A1")
    assert json.loads(row[3])["stop_loss_pct"] == 15
    reasons = [r[0] for r in conn.execute(
        "SELECT changed_because FROM ballots WHERE changed_because IS NOT NULL")]
    assert len(reasons) == 2
    assert [h[1] for h in history(conn, run.paper_id)][-1] == "decided"


async def test_lone_question_gets_a_printed_report(cfg, conn, monkeypatch):
    ask = {"action": "request_info", "target": "risk", "request": "how liquid is it?"}
    scripted_committee(monkeypatch, {
        "aggressive": [{"action": "pass"}], "conservative": [{"action": "pass"}],
        "neutral": [ask, {"action": "pass", "changed_because": "risk report answered it"}],
    })
    run = await run_paper(cfg, conn, "NVDA", AS_OF, save=True)
    first = run.debate.rounds[0]
    assert first.outcome.kind == "request_info" and first.reports[0]["team"] == "risk"
    assert first.reports[0]["report"]["findings"]            # a real (fake-model) report
    assert run.outcome == "pass"
    stored = conn.execute("SELECT team, questions, report_json FROM info_requests").fetchone()
    assert stored[0] == "risk" and "how liquid" in stored[1]


async def test_stubborn_committee_is_shelved_at_the_round_limit(cfg, conn, monkeypatch):
    from dataclasses import replace

    scripted_committee(monkeypatch, {"aggressive": [{"action": "invest", "size_usd": 9000}],
                                     "conservative": [{"action": "pass"}],
                                     "neutral": [{"action": "pass"}]})
    short = replace(cfg, committee=replace(cfg.committee, max_rounds=4))
    run = await run_paper(short, conn, "NVDA", AS_OF, save=True)
    assert run.outcome == "no_consensus" and len(run.debate.rounds) == 4
    assert [h[1] for h in history(conn, run.paper_id)][-1] == "shelved"


# ------------------------------------------------------------------ CLI and UI

def write_config(cfg, tmp_path) -> str:
    path = tmp_path / "functional.toml"
    path.write_text(f'[paths]\ndb = "{cfg.db_path}"\n[data]\ncsv_dir = "{cfg.csv_dir}"\n')
    return str(path)


def test_cli_debate_eval_and_ask_team(cfg, tmp_path, capsys):
    config = write_config(cfg, tmp_path)
    assert cli.main(["--config", config, "debate", "NVDA", "--as-of", "2026-10-09",
                     "--save"]) == 0
    out = capsys.readouterr().out
    assert "Assessment proposal: submit" in out and "OUTCOME: invest" in out
    assert cli.main(["--config", config, "eval", "--symbols", "AAPL", "--as-of", "2026-10-09",
                     "--out", str(tmp_path / "eval")]) == 0
    assert '"avg_rounds_to_conclusion": 3' in capsys.readouterr().out
    assert cli.main(["--config", config, "eval", "--replay-last", "5"]) == 0
    replayed = capsys.readouterr().out
    assert "Concluded: 2" in replayed and "| pass_by_majority | 2 |" in replayed
    assert cli.main(["--config", config, "ask-team", "assessment", "NVDA", "--as-of",
                     "2026-10-09", "--question", "valuation?"]) == 0
    assert "tool calculate(" in capsys.readouterr().out


async def test_ui_shows_a_saved_debate_with_reports(cfg, conn):
    from qfii.web.app import build_app

    await run_paper(cfg, conn, "NVDA", AS_OF, save=True)
    server = build_app(cfg).state.server
    listed = server.debates()
    detail = server.debate(listed[0]["id"])
    assert detail["proposal"]["verdict"] == "submit"
    assert detail["rounds"][0]["reports"][0]["team"] == "risk"
    assert [r["result"] for r in detail["rounds"]] == ["request_info", "continue", "invest"]
    assert detail["research"]["observations"]


# ------------------------------------------------------------------ what-if replay

HOLDOUT_PLAN = {"aggressive": [{"action": "invest", "size_usd": 9000}],
                "conservative": [{"action": "pass"}],
                "neutral": [{"action": "request_info", "target": "risk", "request": "liquidity?"},
                            {"action": "pass", "changed_because": "the report"}]}


async def test_replay_predicts_what_the_engine_does_under_other_rules(cfg, conn, monkeypatch):
    from dataclasses import replace

    from qfii.committee.replay import from_records, replay_debate

    scripted_committee(monkeypatch, HOLDOUT_PLAN)
    base = replace(cfg, committee=replace(cfg.committee, max_rounds=12))
    run = await run_paper(base, conn, "NVDA", AS_OF, save=True)
    assert run.outcome == "no_consensus" and len(run.debate.rounds) == 12
    rounds, kinds = from_records(run.debate.rounds)
    for change in ({"pass_rule": "majority"}, {"stop_after_unchanged_rounds": 3},
                   {"stop_after_unchanged_rounds": 2, "pass_rule": "unanimous"},
                   {"majority_pass_after_unchanged_rounds": 2},
                   {"majority_pass_after_unchanged_rounds": 4,
                    "stop_after_unchanged_rounds": 4}):
        rules = replace(base.committee, **change)
        predicted = replay_debate(rounds, kinds, rules)
        real = await run_paper(replace(base, committee=rules), conn, "NVDA", AS_OF, save=True)
        assert predicted.exact
        assert (predicted.outcome, predicted.rounds) == \
            ("no_consensus" if real.outcome == "no_consensus" else real.outcome,
             len(real.debate.rounds)), change


async def test_eval_reports_what_if_and_replays_from_the_database(cfg, conn, monkeypatch,
                                                                tmp_path):
    from dataclasses import replace

    from qfii.evaluate import replay_saved

    scripted_committee(monkeypatch, HOLDOUT_PLAN)
    base = replace(cfg, committee=replace(cfg.committee, max_rounds=8))
    result = await evaluate(base, ["NVDA"], AS_OF, tmp_path)
    row = result["papers"][0]
    assert row["outcome"] == "no_consensus" and row["final_votes"] == {"invest": 1, "pass": 2}
    assert row["what_if"]["pass_by_majority"] == {"outcome": "pass", "rounds": 2, "exact": True,
                                                  "size_usd": None, "amendment_id": None}
    w = result["summary"]["what_if"]
    assert w["pass_by_majority"]["rounds_saved"] == 6
    assert w["stop_after_3_unchanged"]["avg_rounds_all_debated"] == 5
    report = (Path(result["dir"]) / "report.md").read_text()
    assert "## What if" in report and "NVDA: {'invest': 1, 'pass': 2}" in report
    assert "| NVDA | no_consensus / 8 | pass / 2 | no_consensus / 5 | pass / 4 | pass / 5 |" \
        in report

    # the same numbers straight from the saved ballots
    pid = conn.execute("SELECT MAX(paper_id) FROM debates").fetchone()[0]
    saved = replay_saved(conn, base.committee, [pid])
    assert saved["papers"][0]["what_if"] == row["what_if"]
    assert saved["papers"][0]["rounds_total"] == 8
    assert saved["summary"]["what_if"] == w
