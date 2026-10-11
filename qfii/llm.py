"""Provider-agnostic LLM access, one model tier per agent.

FakeLLM runs everything offline; OpenAICompatLLM talks to any OpenAI-compatible
/chat/completions endpoint (NVIDIA NIM on Brev, Ollama, vLLM, OpenAI).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .config import Config


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int


class LLM(Protocol):
    provider: str
    model: str

    async def complete(self, system: str, user: str) -> LLMResult: ...


class LLMError(RuntimeError):
    pass


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class FakeLLM:
    """Scripted model: `responder(system, user) -> reply text`."""

    provider = "fake"

    def __init__(self, responder: Callable[[str, str], str], model: str = "fake"):
        self._responder = responder
        self.model = model
        self.prompts: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str) -> LLMResult:
        self.prompts.append((system, user))
        text = self._responder(system, user)
        return LLMResult(text, self.provider, self.model, _approx_tokens(system + user),
                         _approx_tokens(text))


class OpenAICompatLLM:
    provider = "openai_compat"

    def __init__(self, base_url: str, model: str, api_key: str | None = None,
                 http_client: httpx.AsyncClient | None = None, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._http = http_client
        self._timeout = timeout

    async def complete(self, system: str, user: str) -> LLMResult:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        client = self._http or httpx.AsyncClient(timeout=self._timeout)
        try:
            resp = await client.post(f"{self.base_url}/chat/completions", json=body,
                                     headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"{self.base_url}: {exc}") from exc
        finally:
            if self._http is None:
                await client.aclose()
        if resp.status_code != 200:
            raise LLMError(f"{self.base_url}: HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected response shape: {str(data)[:300]}") from exc
        usage = data.get("usage") or {}
        return LLMResult(text, self.provider, self.model,
                         int(usage.get("prompt_tokens") or _approx_tokens(system + user)),
                         int(usage.get("completion_tokens") or _approx_tokens(text)))


def make_llm(cfg: Config, agent: str, fake_responder: Callable[[str, str], str]) -> LLM:
    spec = cfg.model_for(agent)
    if spec.provider == "fake":
        return FakeLLM(fake_responder, spec.model)
    api_key = os.environ.get(spec.api_key_env) if spec.api_key_env else None
    if spec.api_key_env and not api_key:
        raise LLMError(f"environment variable {spec.api_key_env} is not set")
    return OpenAICompatLLM(spec.base_url, spec.model, api_key)


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a model reply as one JSON object, tolerating ```json fences."""
    cleaned = _FENCE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end < start:
        raise ValueError("reply contains no JSON object")
    value = json.loads(cleaned[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("reply is not a JSON object")  # noqa: TRY004 - callers catch ValueError
    return value
