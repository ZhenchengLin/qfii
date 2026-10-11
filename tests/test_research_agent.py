import json
from datetime import date

import pytest

from qfii.agents.research.agent import ResearchAgent, ResearchFailed
from qfii.agents.research.fake import fake_research_responder
from qfii.contracts import EvidencePacket, ResearchRequest
from qfii.db import connect, init_db
from qfii.llm import FakeLLM, LLMError
from qfii.store import save_packet

REQ = ResearchRequest(symbol="AAPL", as_of=date(2026, 10, 9))



class Script:
    """FakeLLM responder that plays replies in order; a reply may be a function."""

    def __init__(self, *replies):
        self.replies = list(replies)

    def __call__(self, system, user):
        r = self.replies.pop(0)
        return r(system, user) if callable(r) else r


async def test_fake_llm_produces_cited_packet(cfg):
    packet, calls = await ResearchAgent(cfg).run(REQ)
    assert isinstance(packet, EvidencePacket)
    assert packet.symbol == "AAPL" and packet.as_of == "2026-10-09"
    ids = {e.id for e in packet.evidence}
    assert packet.observations and all(set(o.evidence_ids) <= ids for o in packet.observations)
    assert "synthetic_data" in packet.quality_flags
    assert "no_fundamentals" in packet.quality_flags
    assert [c["ok"] for c in calls] == [True]


async def test_prompt_contains_only_facts_not_raw_series(cfg):
    llm = FakeLLM(fake_research_responder)
    await ResearchAgent(cfg, llm).run(REQ)
    system, user = llm.prompts[0]
    assert "You do not recommend" in system
    body = json.loads(user)
    assert body["symbol"] == "AAPL" and "bars" not in user
    assert {f["name"] for f in body["facts"]} >= {"last_close", "rsi_14"}


async def test_invalid_reply_is_retried_with_feedback(cfg):
    llm = FakeLLM(Script("not json at all", fake_research_responder))
    packet, calls = await ResearchAgent(cfg, llm).run(REQ)
    assert [c["ok"] for c in calls] == [False, True]
    assert "your_previous_reply_was_rejected_because" in llm.prompts[1][1]
    assert packet.observations


async def test_hallucinated_citation_rejected(cfg):
    fake = json.dumps({"observations": [{"specialty": "technical", "text": "Price rose.",
                                         "evidence_ids": ["ev_made_up"]}]})
    with pytest.raises(ResearchFailed, match="ev_made_up") as info:
        await ResearchAgent(cfg, FakeLLM(Script(fake, fake))).run(REQ)
    assert len(info.value.llm_calls) == 2


async def test_llm_outage_fails_cleanly(cfg):
    class Down:
        provider, model = "openai_compat", "x"

        async def complete(self, system, user, schema=None):
            raise LLMError("connection refused")

    with pytest.raises(ResearchFailed, match="connection refused"):
        await ResearchAgent(cfg, Down()).run(REQ)


async def test_save_packet(cfg):
    packet, calls = await ResearchAgent(cfg).run(REQ)
    conn = connect(cfg.db_path)
    init_db(conn, 1000)
    paper_id = save_packet(conn, REQ, packet, calls, "mid")
    assert conn.execute("SELECT state FROM papers WHERE id=?", (paper_id,)).fetchone()[0] \
        == "packet_ready"
    assert [r[0] for r in conn.execute("SELECT to_state FROM events ORDER BY id")] \
        == ["candidate", "packet_ready"]
    assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == len(packet.evidence)
    # same facts again: evidence is deduplicated, a second paper is created
    save_packet(conn, REQ, packet, calls, "mid")
    assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == len(packet.evidence)
    assert conn.execute("SELECT COUNT(*) FROM packets").fetchone()[0] == 2
    assert tuple(conn.execute("SELECT agent, ok FROM llm_calls").fetchone()) == ("research", 1)
