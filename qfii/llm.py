"""Provider-agnostic LLM access, one model tier per agent.

  fake           scripted replies, runs everything offline
  ollama         local models via Ollama's native /api/chat (thinking kept separate,
                 JSON-schema output, per-request context size, unload for swapping)
  openai_compat  any OpenAI-compatible /chat/completions endpoint (vLLM, NIM, OpenAI)

All calls go through the one-model-at-a-time scheduler (see scheduler.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol

import httpx

from .config import Config, ModelSpec
from .scheduler import SchedulerSettings, scheduler_for_loop

OLLAMA_DEFAULT_URL = "http://localhost:11434"


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float = 0.0
    thinking: str = ""

    def record(self, ok: bool = True, error: str | None = None) -> dict[str, Any]:
        """The dict agents keep per call; store.record_llm_calls persists it."""
        return {"ok": ok, "error": error, "provider": self.provider, "model": self.model,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens, "cost_usd": self.cost_usd}


def failed_call(llm: LLM, error: str) -> dict[str, Any]:
    return {"ok": False, "error": error, "provider": llm.provider, "model": llm.model,
            "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}


class LLM(Protocol):
    provider: str
    model: str

    async def complete(self, system: str, user: str,
                       schema: dict[str, Any] | None = None) -> LLMResult: ...


class LLMError(RuntimeError):
    pass


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class _Priced:
    usd_per_m_input = 0.0
    usd_per_m_output = 0.0

    def _cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return round((prompt_tokens * self.usd_per_m_input
                      + completion_tokens * self.usd_per_m_output) / 1_000_000, 8)


class FakeLLM(_Priced):
    """Scripted model: `responder(system, user) -> reply text`."""

    provider = "fake"

    def __init__(self, responder: Callable[[str, str], str], model: str = "fake"):
        self._responder = responder
        self.model = model
        self.prompts: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str,
                       schema: dict[str, Any] | None = None) -> LLMResult:
        self.prompts.append((system, user))
        text = self._responder(system, user)
        pt, ct = _approx_tokens(system + user), _approx_tokens(text)
        return LLMResult(text, self.provider, self.model, pt, ct, self._cost(pt, ct))


def _messages(system: str, user: str, merge: bool) -> list[dict[str, str]]:
    # DeepSeek-R1 distills are trained without a system prompt: fold it into the user turn
    if merge:
        return [{"role": "user", "content": f"{system}\n\n---\n\n{user}"}]
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


class OllamaLLM(_Priced):
    provider = "ollama"

    def __init__(self, model: str, base_url: str = "", num_ctx: int = 16384,
                 think: bool = False, temperature: float = 0.0, merge_system: bool = False,
                 timeout_s: float = 600.0, http_client: httpx.AsyncClient | None = None,
                 max_output_tokens: int = 4096):
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.base_url = (base_url or OLLAMA_DEFAULT_URL).rstrip("/")
        self.num_ctx = num_ctx
        self.think = think
        self.temperature = temperature
        self.merge_system = merge_system
        self._timeout = timeout_s
        self._http = http_client

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        client = self._http or httpx.AsyncClient(timeout=self._timeout)
        try:
            resp = await client.post(f"{self.base_url}{path}", json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"ollama {self.base_url}: {exc!r}") from exc
        finally:
            if self._http is None:
                await client.aclose()
        if resp.status_code != 200:
            raise LLMError(f"ollama {self.model}: HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def complete(self, system: str, user: str,
                       schema: dict[str, Any] | None = None) -> LLMResult:
        body: dict[str, Any] = {
            "model": self.model, "stream": False, "think": self.think,
            "messages": _messages(system, user, self.merge_system),
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx,
                        "num_predict": self.max_output_tokens},
            "keep_alive": "30m",
        }
        if schema is not None:
            body["format"] = schema
        data = await self._post("/api/chat", body)
        message = data.get("message") or {}
        text = message.get("content", "")
        pt = int(data.get("prompt_eval_count") or _approx_tokens(system + user))
        ct = int(data.get("eval_count") or _approx_tokens(text))
        # Ollama silently drops the start of a prompt longer than num_ctx: refuse instead
        if pt >= self.num_ctx - 8:
            raise LLMError(f"prompt of ~{pt} tokens filled the {self.num_ctx}-token context of "
                           f"{self.model}; raise num_ctx or shorten the input")
        thinking = message.get("thinking") or ""
        if not thinking:
            text, thinking = split_thinking(text)
        if data.get("done_reason") == "length" and not text.strip():
            raise LLMError(f"{self.model} used all {self.max_output_tokens} output tokens "
                           f"(thinking {len(thinking)} chars) without answering")
        return LLMResult(text, self.provider, self.model, pt, ct, self._cost(pt, ct), thinking)

    async def unload(self, timeout_s: float = 60.0) -> None:
        """Evict the model from memory and wait until Ollama reports it gone."""
        await self._post("/api/generate", {"model": self.model, "keep_alive": 0})
        client = self._http or httpx.AsyncClient(timeout=10)
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_s
            while loop.time() < deadline:
                ps = (await client.get(f"{self.base_url}/api/ps")).json().get("models", [])
                if not any(m.get("name") == self.model for m in ps):
                    return
                await asyncio.sleep(0.25)
        finally:
            if self._http is None:
                await client.aclose()
        raise LLMError(f"ollama did not unload {self.model} within {timeout_s}s")


class OpenAICompatLLM(_Priced):
    provider = "openai_compat"

    def __init__(self, base_url: str, model: str, api_key: str | None = None,
                 http_client: httpx.AsyncClient | None = None, timeout: float = 600.0,
                 temperature: float = 0.0, merge_system: bool = False,
                 max_output_tokens: int = 4096):
        self.max_output_tokens = max_output_tokens
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._http = http_client
        self._timeout = timeout
        self.temperature = temperature
        self.merge_system = merge_system

    async def complete(self, system: str, user: str,
                       schema: dict[str, Any] | None = None) -> LLMResult:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        client = self._http or httpx.AsyncClient(timeout=self._timeout)
        try:
            # Newer models reject some parameters (temperature, max_tokens, json_schema).
            # Drop whatever the server names in a 400 and try again; remember it.
            for _ in range(4):
                body = self._body(system, user, schema)
                try:
                    resp = await client.post(f"{self.base_url}/chat/completions", json=body,
                                             headers=headers)
                except httpx.HTTPError as exc:
                    raise LLMError(f"{self.base_url}: {exc!r}") from exc
                if resp.status_code == 400 and self._adapt(resp.text):
                    continue
                break
        finally:
            if self._http is None:
                await client.aclose()
        if resp.status_code != 200:
            raise LLMError(f"{self.base_url}: HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected response shape: {str(data)[:300]}") from exc
        if not text.strip() and choice.get("finish_reason") == "length":
            # reasoning models spend the output budget on hidden reasoning first
            raise LLMError(f"{self.model} used all {self.max_output_tokens} output tokens "
                           f"(including hidden reasoning) without answering; raise "
                           f"max_output_tokens")
        usage = data.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or _approx_tokens(system + user))
        ct = int(usage.get("completion_tokens") or _approx_tokens(text))
        text, thinking = split_thinking(text)
        return LLMResult(text, self.provider, self.model, pt, ct, self._cost(pt, ct), thinking)

    _unsupported: set[str]

    def _body(self, system: str, user: str, schema: dict[str, Any] | None) -> dict[str, Any]:
        drop = getattr(self, "_unsupported", set())
        body: dict[str, Any] = {"model": self.model,
                                "messages": _messages(system, user, self.merge_system)}
        if "temperature" not in drop:
            body["temperature"] = self.temperature
        token_key = "max_completion_tokens" if "max_tokens" in drop else "max_tokens"
        body[token_key] = self.max_output_tokens
        if schema is not None and "json_schema" not in drop:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "reply", "schema": schema}}
        elif "response_format" not in drop:
            body["response_format"] = {"type": "json_object"}
        return body

    def _adapt(self, error_text: str) -> bool:
        drop = self.__dict__.setdefault("_unsupported", set())
        text = error_text.lower()
        for param in ("temperature", "max_tokens", "json_schema", "response_format"):
            if param in text and param not in drop:
                drop.add(param)
                return True
        return False


class ScheduledLLM:
    """Routes every call through the per-loop one-model-at-a-time scheduler."""

    def __init__(self, inner: Any, settings: SchedulerSettings, json_schema: bool = True):
        self.inner = inner
        self.json_schema = json_schema
        self.settings = settings
        self.provider = inner.provider
        self.model = inner.model
        self.key = f"{inner.provider}:{getattr(inner, 'base_url', '')}:{inner.model}"

    async def complete(self, system: str, user: str,
                       schema: dict[str, Any] | None = None) -> LLMResult:
        sched = scheduler_for_loop(self.settings)
        if hasattr(self.inner, "unload"):
            sched.register_unloader(self.key, lambda _key: self.inner.unload())
        schema = schema if self.json_schema else None
        return await sched.run(self.key, lambda: self.inner.complete(system, user, schema))

    def __getattr__(self, name: str) -> Any:  # prompts, usd_per_m_*, etc.
        if name == "inner":
            raise AttributeError(name)
        return getattr(self.inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("usd_per_m_"):
            setattr(self.inner, name, value)
        else:
            object.__setattr__(self, name, value)


def model_label(spec: ModelSpec) -> str:
    """Display name of a model, resolving env: values when they are set."""
    try:
        return resolve_env(spec.model)
    except LLMError:
        return spec.model


def resolve_env(value: str) -> str:
    """Config values like "env:LLM_MODEL" are read from the environment, so lab or
    cloud settings never have to be written into a config file."""
    if value.startswith("env:"):
        name = value[4:]
        resolved = os.environ.get(name)
        if not resolved:
            raise LLMError(f"environment variable {name} is not set")
        return resolved
    return value


def build_llm(spec: ModelSpec, fake_responder: Callable[[str, str], str]) -> Any:
    spec = replace(spec, model=resolve_env(spec.model), base_url=resolve_env(spec.base_url))
    if spec.provider == "fake":
        llm: Any = FakeLLM(fake_responder, spec.model)
    elif spec.provider == "ollama":
        llm = OllamaLLM(spec.model, spec.base_url, spec.num_ctx, spec.think, spec.temperature,
                        spec.merge_system_prompt, spec.timeout_s,
                        max_output_tokens=spec.max_output_tokens)
    else:
        api_key = os.environ.get(spec.api_key_env) if spec.api_key_env else None
        if spec.api_key_env and not api_key:
            raise LLMError(f"environment variable {spec.api_key_env} is not set")
        llm = OpenAICompatLLM(spec.base_url, spec.model, api_key, timeout=spec.timeout_s,
                              temperature=spec.temperature, merge_system=spec.merge_system_prompt,
                              max_output_tokens=spec.max_output_tokens)
    llm.usd_per_m_input = spec.usd_per_m_input
    llm.usd_per_m_output = spec.usd_per_m_output
    return llm


def make_llm(cfg: Config, agent: str, fake_responder: Callable[[str, str], str]) -> LLM:
    spec = cfg.model_for(agent)
    return ScheduledLLM(build_llm(spec, fake_responder), cfg.scheduler, spec.json_schema)


_THINK = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def split_thinking(text: str) -> tuple[str, str]:
    """Separate <think>...</think> reasoning (DeepSeek-R1 style) from the answer."""
    thoughts = _THINK.findall(text)
    answer = _THINK.sub("", text)
    if "</think>" in answer:  # opening tag was in the prompt template
        head, _, answer = answer.partition("</think>")
        thoughts.insert(0, head)
    return answer.strip(), "\n".join(t.strip() for t in thoughts)


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a model reply as one JSON object, tolerating fences and think blocks."""
    cleaned = _FENCE.sub("", split_thinking(text)[0])
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end < start:
        raise ValueError("reply contains no JSON object")
    value = json.loads(cleaned[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("reply is not a JSON object")  # noqa: TRY004 - callers catch ValueError
    return value
