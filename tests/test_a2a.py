import httpx
import pytest

from qfii.a2a_client import AgentError, ask_agent
from qfii.agents.research.a2a import build_research_app
from qfii.agents.research.agent import ResearchAgent
from qfii.contracts import EvidencePacket
from qfii.db import connect

URL = "http://research.test/"


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL)


async def test_agent_card_served(cfg):
    app = build_research_app(ResearchAgent(cfg), URL)
    async with client_for(app) as http:
        card = (await http.get("/.well-known/agent-card.json")).json()
    assert card["name"] == "Qfii Research"
    assert card["skills"][0]["id"] == "research_symbol"
    assert card["supportedInterfaces"][0]["url"] == URL


async def test_research_over_a2a_and_saved(cfg):
    app = build_research_app(ResearchAgent(cfg), URL, cfg.db_path)
    async with client_for(app) as http:
        reply = await ask_agent(URL, {"symbol": "nvda", "as_of": "2026-10-09"}, http)
    packet = EvidencePacket.model_validate(reply["packet"])
    assert packet.symbol == "NVDA" and reply["paper_id"] == 1
    conn = connect(cfg.db_path)
    assert tuple(conn.execute("SELECT symbol, state FROM papers").fetchone()) == ("NVDA", "packet_ready")


async def test_bad_request_returns_error(cfg):
    app = build_research_app(ResearchAgent(cfg), URL)
    async with client_for(app) as http:
        with pytest.raises(AgentError, match="bad request"):
            await ask_agent(URL, {"symbol": "!!"}, http)
        with pytest.raises(AgentError, match="bad request"):
            await ask_agent(URL, {"ticker": "AAPL"}, http)
