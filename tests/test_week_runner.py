"""The week replay end to end, on a small synthetic dataset with scripted models."""

import asyncio
import json
from datetime import date, timedelta

import pytest

from qfii.config import load_config
from qfii.db import connect, init_db
from qfii.papers import state_of
from qfii.week.runner import Week

DAYS = ("2026-10-05", "2026-10-06")
TREND = {"AAA": 0.002, "BBB": 0.0, "CCC": -0.001, "SPY": 0.0005}


def make_dataset(root, intraday_moves=None):
    """3 stocks + SPY: a year of daily closes, two days of 10-minute bars, and an
    8-K for AAA accepted on day 2 at 11:05 ET."""
    intraday_moves = intraday_moves or {}
    (root / "prices").mkdir(parents=True)
    (root / "intraday").mkdir()
    for sym, drift in TREND.items():
        rows, price, d = [], 100.0, date(2025, 8, 1)
        while d < date(2026, 10, 5):
            if d.weekday() < 5:
                price *= 1 + drift
                rows.append(f"{d},{price:.4f},1000000")
            d += timedelta(days=1)
        (root / "prices" / f"{sym}.csv").write_text("date,close,volume\n" + "\n".join(rows))
        bars = []
        for day in DAYS:
            for i in range(39):
                minute = 9 * 60 + 40 + 10 * i
                p = price * (1 + intraday_moves.get((sym, day, i), 0))
                bars.append(f"{day}T{minute // 60:02d}:{minute % 60:02d}:00-04:00,"
                            f"{p:.4f},{p:.4f},{p:.4f},{p:.4f},50000")
        (root / "intraday" / f"{sym}.csv").write_text(
            "time,open,high,low,close,volume\n" + "\n".join(bars))
    for sym in ("AAA", "BBB", "CCC"):
        (root / "sec" / sym).mkdir(parents=True)
        filings = []
        if sym == "AAA":
            filings = [{"form": "8-K", "accession": "aaa-8k", "filing_date": DAYS[1],
                        "accepted": f"{DAYS[1]}T11:05:00"}]
            (root / "sec" / sym / "aaa-8k.meta.json").write_text(json.dumps(
                {"form": "8-K", "filed": DAYS[1], "period": DAYS[1], "accession": "aaa-8k",
                 "url": "https://sec.example/aaa"}))
            (root / "sec" / sym / "aaa-8k.8k.json").write_text(json.dumps(
                {"items": [{"code": "2.02", "label": "results of operations (earnings)"}],
                 "headline": "AAA raises guidance",
                 "text": "AAA Corp raises its full-year revenue guidance."}))
        (root / "sec" / sym / "filings.json").write_text(json.dumps(
            {"symbol": sym, "entity": f"{sym} Corp", "filings": filings}))
    (root / "manifest.json").write_text(json.dumps({
        "symbols": ["AAA", "BBB", "CCC"], "names": {}, "benchmark": "SPY",
        "window": {"market_days": list(DAYS)},
        "storage": {"bytes_per_market_day": 1000}}))


@pytest.fixture
def week_cfg(tmp_path):
    def build(gb_per_day=1.0, moves=None, extra=""):
        data = tmp_path / "week"
        make_dataset(data, moves)
        path = tmp_path / "week.toml"
        path.write_text(f'''
[paths]
db = "{tmp_path / "week.db"}"
[data]
source = "csv"
csv_dir = "{data / "prices"}"
intraday_dir = "{data / "intraday"}"
macro_dir = "{tmp_path / "macro"}"
sec_cache_dir = "{data / "sec"}"
sec_user_agent = "Qfii test@example.com"
[committee]
neutral_is_assessment = true
[week]
start = "{DAYS[0]}"
end = "{DAYS[1]}"
dataset_dir = "{data}"
gb_per_day = {gb_per_day}
{extra}
''')
        cfg = load_config(path)
        conn = connect(cfg.db_path)
        init_db(conn, cfg.limits.starting_cash_usd)
        return cfg, conn
    return build


def run(cfg, conn, **kw):
    events = []
    week = Week(cfg, conn, events.append)
    return asyncio.run(week.run(**kw)), events, week


def kinds(events, kind, symbol=None):
    return [e for e in events if e["kind"] == kind and (symbol is None or e["symbol"] == symbol)]


def test_first_slot_researches_screens_and_debates(week_cfg):
    cfg, conn = week_cfg()
    summary, events, _ = run(cfg, conn, max_slots=1)
    assert sorted(e["symbol"] for e in kinds(events, "research")) == ["AAA", "BBB", "CCC"]
    screen = kinds(events, "screen")[0]
    calls = {i["symbol"]: i["decision"] for i in screen["items"]}
    assert calls == {"AAA": "advance", "BBB": "hold", "CCC": "dig"}   # scripted Assessment
    debate = kinds(events, "debate", "AAA")[0]
    assert debate["outcome"] in ("invest", "pass", "no_consensus") and debate["debate_id"]
    assert summary["last_slot"] == "2026-10-05T09:40"


def test_reports_rewritten_only_on_new_information(week_cfg):
    cfg, conn = week_cfg(moves={("BBB", DAYS[0], 3): 0.03, ("BBB", DAYS[0], 4): 0.03})
    _, events, _ = run(cfg, conn, until="2026-10-05T10:30")
    reasons = [(e["slot"][11:], e["symbol"], e["reason"]) for e in kinds(events, "research")]
    assert ("09:50", "CCC", "Assessment asked: revenue guidance next quarter") in reasons
    assert ("10:10", "BBB", "price moved +3.0% since the report") in reasons
    # quiet stocks keep their 09:40 report all morning
    assert [r for r in reasons if r[1] == "AAA"] == [("09:40", "AAA", "first report")]


def test_debate_once_a_day_unless_something_is_filed(week_cfg):
    cfg, conn = week_cfg()
    _, events, _ = run(cfg, conn, until="2026-10-06T11:30")
    debates = [(e["slot"], e["outcome"]) for e in kinds(events, "debate", "AAA")]
    days = [s[:10] for s, _ in debates]
    assert days.count(DAYS[0]) == 1                       # once on day 1
    assert kinds(events, "debate_skipped", "AAA")         # advanced again, skipped
    # day 2: once in the morning, and again after the 8-K accepted at 11:05
    day2 = [s for s, _ in debates if s.startswith(DAYS[1])]
    assert day2[0] == "2026-10-06T09:40" and "2026-10-06T11:10" in day2
    filing = [e for e in kinds(events, "research", "AAA") if e["slot"] == "2026-10-06T11:10"]
    assert filing and filing[0]["reason"] == "new filing: 8-K accepted 2026-10-06 11:05"


def test_superseded_reports_are_dropped_and_debated_papers_kept(week_cfg):
    cfg, conn = week_cfg()
    _, events, _ = run(cfg, conn, until="2026-10-05T10:00")
    ccc = [e["paper_id"] for e in kinds(events, "research", "CCC")]
    assert len(ccc) == 2 and state_of(conn, ccc[0]) == "dropped"
    aaa = kinds(events, "debate", "AAA")[0]["paper_id"]
    assert state_of(conn, aaa) in ("decided", "passed", "shelved")


def test_resume_continues_where_it_stopped(week_cfg):
    cfg, conn = week_cfg()
    first, _, _ = run(cfg, conn, max_slots=3)
    assert first["status"] == "stopped" and first["last_slot"] == "2026-10-05T10:00"
    again, events, week = run(cfg, conn, resume=True, max_slots=2)
    assert again["run_id"] == first["run_id"] and again["last_slot"] == "2026-10-05T10:20"
    assert {e["slot"] for e in events} <= {"2026-10-05T10:10", "2026-10-05T10:20"}
    assert not [e for e in kinds(events, "research") if e["reason"] == "first report"]
    assert week.tracked["AAA"].debated_day == DAYS[0]      # state survived the restart


def test_stops_when_a_day_writes_more_than_the_limit(week_cfg):
    cfg, conn = week_cfg(gb_per_day=0.01)
    manifest = json.loads((cfg.week.dataset_dir / "manifest.json").read_text())
    manifest["storage"]["bytes_per_market_day"] = 20 * 1024 ** 2   # 20 MB > 10 MB limit
    (cfg.week.dataset_dir / "manifest.json").write_text(json.dumps(manifest))
    summary, events, _ = run(cfg, conn)
    assert summary["status"] == "stopped" and "GB/day limit" in summary["note"]
    assert kinds(events, "storage") and summary["last_slot"] == "2026-10-05T09:40"


def test_neutral_member_is_assessment_and_asks_research(week_cfg, monkeypatch):
    from qfii.committee import fake as committee_fake
    from qfii.committee.members import build_members, read_member_prompt

    cfg, conn = week_cfg()
    neutral = next(m for m in build_members(cfg) if m.name == "neutral")
    assert "You are also the ASSESSMENT team" in neutral.persona

    def factory(member):
        def respond(system, user):
            view = read_member_prompt(user)
            r = len(view["previous_rounds"])
            if member == "neutral" and r == 0:
                return json.dumps({"argument": "check the filings", "action": "request_info",
                                   "target": "research", "request": "revenue guidance"})
            return json.dumps({"argument": "no", "action": "pass",
                               **({"changed_because": "the research report"}
                                  if member == "neutral" else {})})
        return respond
    monkeypatch.setattr(committee_fake, "fake_member_responder", factory)
    monkeypatch.setattr("qfii.committee.members.fake_member_responder", factory)
    _, events, _ = run(cfg, conn, max_slots=1)
    debate_id = kinds(events, "debate", "AAA")[0]["debate_id"]
    team, questions, report = conn.execute(
        "SELECT team, questions, report_json FROM info_requests WHERE debate_id = ?",
        (debate_id,)).fetchone()
    assert team == "research" and "revenue guidance" in questions
    assert json.loads(report)["observations"]


def test_asking_assessment_is_refused_when_it_sits_at_the_table(week_cfg, monkeypatch):
    from qfii.committee import fake as committee_fake

    cfg, conn = week_cfg()

    def factory(member):
        def respond(system, user):
            return json.dumps({"argument": "x", "action": "request_info",
                               "target": "assessment", "request": "why?"})
        return respond
    monkeypatch.setattr(committee_fake, "fake_member_responder", factory)
    monkeypatch.setattr("qfii.committee.members.fake_member_responder", factory)
    _, events, _ = run(cfg, conn, max_slots=1)
    debate_id = kinds(events, "debate", "AAA")[0]["debate_id"]
    actions = {r[0] for r in conn.execute("SELECT action FROM ballots WHERE debate_id = ?",
                                          (debate_id,))}
    assert actions == {"abstain"}          # invalid twice -> abstain, never a team request
