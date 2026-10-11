"""Minimal A2A client: send one JSON payload to an agent, get its JSON reply."""

from __future__ import annotations

from typing import Any

import httpx
from a2a.client.client import ClientConfig
from a2a.client.client_factory import create_client
from a2a.helpers.proto_helpers import get_data_parts, new_data_message
from a2a.types import Role, SendMessageRequest


class AgentError(RuntimeError):
    pass


async def ask_agent(url: str, payload: dict[str, Any],
                    http_client: httpx.AsyncClient | None = None,
                    timeout: float = 300.0) -> dict[str, Any]:
    http = http_client or httpx.AsyncClient(timeout=timeout)
    try:
        client = await create_client(url, client_config=ClientConfig(httpx_client=http,
                                                                     streaming=False))
        message = new_data_message(payload, role=Role.ROLE_USER)
        async for response in client.send_message(SendMessageRequest(message=message)):
            if response.HasField("message"):
                data = get_data_parts(response.message.parts)
                if not data:
                    raise AgentError("agent replied without a data part")
                reply = data[0]
                if "error" in reply:
                    raise AgentError(reply["error"])
                return reply
            if response.HasField("task"):
                raise AgentError(f"agent returned task in state "
                                 f"{response.task.status.state} instead of a message")
        raise AgentError("agent sent no response")
    finally:
        if http_client is None:
            await http.aclose()
