"""Research agent entry point: one request in, one evidence packet out."""

from __future__ import annotations

from typing import Any

from ...config import Config
from ...contracts import EvidencePacket, ResearchRequest
from ...llm import LLM, make_llm
from ...tools.client import ToolBox, ToolError, root_cause
from .fake import fake_research_responder
from .graph import build_research_graph


class ResearchFailed(RuntimeError):
    def __init__(self, message: str, llm_calls: list[dict[str, Any]]):
        super().__init__(message)
        self.llm_calls = llm_calls


class ResearchAgent:
    name = "research"

    def __init__(self, cfg: Config, llm: LLM | None = None):
        self.cfg = cfg
        self.llm = llm or make_llm(cfg, self.name, fake_research_responder)

    async def run(self, request: ResearchRequest) -> tuple[EvidencePacket, list[dict[str, Any]]]:
        try:
            async with ToolBox(self.cfg) as tools:
                graph = build_research_graph(tools, self.llm)
                state = await graph.ainvoke({"request": request})
        except (ToolError, BaseExceptionGroup) as exc:
            raise ResearchFailed(f"{request.symbol}: {root_cause(exc)}", []) from exc
        if state.get("packet") is None:
            raise ResearchFailed(state.get("error") or "research failed", state["llm_calls"])
        return state["packet"], state["llm_calls"]
