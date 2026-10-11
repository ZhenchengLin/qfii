"""MCP client used by agents. In-process by default; remote when tools.mcp_url is set."""

from __future__ import annotations

import json
from typing import Any, Self

from mcp.client import Client

from ..config import Config
from .server import build_tool_server


class ToolError(RuntimeError):
    pass


class ToolBox:
    def __init__(self, cfg: Config):
        target = cfg.mcp_url or build_tool_server(cfg)
        self._client = Client(target)
        self.calls: list[dict[str, Any]] = []  # audit trail of tool calls in this session

    async def __aenter__(self) -> Self:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.__aexit__(*exc)

    async def list(self) -> list[str]:
        return [t.name for t in (await self._client.list_tools()).tools]

    async def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        result = await self._client.call_tool(name, arguments)
        text = "".join(getattr(c, "text", "") for c in result.content)
        self.calls.append({"tool": name, "arguments": arguments, "is_error": result.is_error})
        if result.is_error:
            raise ToolError(f"{name} failed: {text}")
        if result.structured_content is not None:
            return dict(result.structured_content)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ToolError(f"{name} returned non-JSON: {text[:200]}") from exc
