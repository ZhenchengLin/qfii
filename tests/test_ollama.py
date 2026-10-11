import json

import httpx
import pytest

from qfii.llm import LLMError, OllamaLLM, parse_json_object, split_thinking


def ollama(handler, **kw):
    return OllamaLLM("qwen3.5:9b", http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)), **kw)


async def test_chat_request_shape_and_separate_thinking():
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"message": {"content": '{"a": 1}', "thinking": "hmm"},
                                         "prompt_eval_count": 120, "eval_count": 9})

    llm = ollama(handler, think=True, num_ctx=8192, merge_system=True)
    result = await llm.complete("sys", "user", {"type": "object"})
    body = seen["body"]
    assert seen["path"] == "/api/chat"
    assert body["think"] is True and body["format"] == {"type": "object"}
    assert body["options"] == {"temperature": 0.0, "num_ctx": 8192}
    assert body["messages"] == [{"role": "user", "content": "sys\n\n---\n\nuser"}]
    assert (result.text, result.thinking, result.prompt_tokens) == ('{"a": 1}', "hmm", 120)


async def test_inline_think_tags_are_split_off():
    reply = "<think>is {this} json? no</think>\n{\"ok\": true}"
    llm = ollama(lambda r: httpx.Response(200, json={"message": {"content": reply}}))
    result = await llm.complete("s", "u")
    assert result.text == '{"ok": true}' and "is {this}" in result.thinking


async def test_truncated_prompt_is_refused():
    llm = ollama(lambda r: httpx.Response(200, json={"message": {"content": "{}"},
                                                     "prompt_eval_count": 4096}), num_ctx=4096)
    with pytest.raises(LLMError, match="filled the 4096-token context"):
        await llm.complete("s", "u")


async def test_unload_waits_until_gone():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/api/ps":
            loaded = [{"name": "qwen3.5:9b"}] if calls.count("/api/ps") < 2 else []
            return httpx.Response(200, json={"models": loaded})
        return httpx.Response(200, json={"done_reason": "unload"})

    await ollama(handler).unload()
    assert calls == ["/api/generate", "/api/ps", "/api/ps"]


def test_split_thinking_and_parse():
    assert split_thinking("<think>a</think>b") == ("b", "a")
    assert split_thinking("reasoning only</think>{}") == ("{}", "reasoning only")
    assert parse_json_object("<think>{not json}</think>```json\n{\"x\": 2}\n```") == {"x": 2}


async def test_ollama_http_error():
    llm = ollama(lambda r: httpx.Response(404, text="model 'x' not found"))
    with pytest.raises(LLMError, match="404"):
        await llm.complete("s", "u")
