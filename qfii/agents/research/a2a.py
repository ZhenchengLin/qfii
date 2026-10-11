"""The Research agent as an A2A server (JSON-RPC over HTTP).

Request:  one data part matching ResearchRequest, or a text part holding a ticker.
Response: one data part, either {"packet": EvidencePacket, "paper_id": int|None}
          or {"error": str}.
"""

from __future__ import annotations

import logging
from pathlib import Path

from a2a.helpers.proto_helpers import get_data_parts, get_text_parts, new_data_message
from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from pydantic import ValidationError
from starlette.applications import Starlette

from ...contracts import ResearchRequest
from ...db import connect, init_db
from ...store import record_llm_calls, save_packet
from .agent import ResearchAgent, ResearchFailed

log = logging.getLogger(__name__)


def research_agent_card(url: str) -> AgentCard:
    return AgentCard(
        name="Qfii Research",
        description="Undergraduate-researcher agent: gathers factual, cited evidence about a "
                    "stock through MCP tools. Never recommends trades.",
        version="0.1.0",
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=url)],
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["application/json", "text/plain"],
        default_output_modes=["application/json"],
        skills=[AgentSkill(
            id="research_symbol",
            name="Research a symbol",
            description="Return an EvidencePacket: cited factual observations, quality flags "
                        "and gaps for one ticker as of a cutoff date.",
            tags=["research", "evidence", "technical"],
            examples=['{"symbol": "AAPL", "as_of": "2026-10-09"}', "MSFT"],
        )],
    )


class ResearchExecutor(AgentExecutor):
    def __init__(self, agent: ResearchAgent, db_path: Path | None = None):
        self.agent = agent
        self.db_path = db_path

    def _parse(self, parts) -> ResearchRequest:
        data = get_data_parts(parts)
        if data:
            return ResearchRequest.model_validate(data[0])
        text = " ".join(get_text_parts(parts)).strip()
        return ResearchRequest(symbol=text)

    async def execute(self, context, event_queue) -> None:
        def reply(payload: dict) -> object:
            return event_queue.enqueue_event(
                new_data_message(payload, context_id=context.context_id))

        try:
            request = self._parse(context.message.parts)
        except (ValidationError, ValueError) as exc:
            await reply({"error": f"bad request: {exc}"})
            return
        tier = self.agent.cfg.agents["research"].tier
        try:
            packet, calls = await self.agent.run(request)
        except ResearchFailed as exc:
            if self.db_path:
                with connect(self.db_path) as conn:
                    record_llm_calls(conn, "research", tier, exc.llm_calls)
            await reply({"error": str(exc)})
            return
        except Exception as exc:  # tool or data failure: report, don't crash the server
            log.exception("research failed")
            await reply({"error": f"{type(exc).__name__}: {exc}"})
            return
        paper_id = None
        if self.db_path:
            conn = connect(self.db_path)
            init_db(conn, self.agent.cfg.limits.starting_cash_usd)
            paper_id = save_packet(conn, request, packet, calls, tier)
            conn.close()
        await reply({"packet": packet.model_dump(mode="json"), "paper_id": paper_id})

    async def cancel(self, context, event_queue) -> None:
        return None


def build_research_app(agent: ResearchAgent, url: str, db_path: Path | None = None) -> Starlette:
    card = research_agent_card(url)
    handler = DefaultRequestHandler(agent_executor=ResearchExecutor(agent, db_path),
                                    task_store=InMemoryTaskStore(), agent_card=card)
    return Starlette(routes=create_agent_card_routes(card) + create_jsonrpc_routes(handler, "/"))
