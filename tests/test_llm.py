import json

import httpx
import pytest

from qfii.llm import LLMError, OpenAICompatLLM, make_llm, parse_json_object


def test_parse_json_object_tolerates_fences():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Sure! {"a": {"b": 2}} done') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        parse_json_object("no json here")


async def test_openai_compat_request_and_usage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = OpenAICompatLLM("http://nim.local/v1/", "meta/llama", "k", http_client=http)
    result = await llm.complete("sys", "user")
    assert seen["url"] == "http://nim.local/v1/chat/completions"
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
    assert (result.text, result.prompt_tokens, result.completion_tokens) == ('{"ok": true}', 11, 3)


async def test_openai_compat_http_error():
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    with pytest.raises(LLMError, match="429"):
        await OpenAICompatLLM("http://x/v1", "m", http_client=http).complete("s", "u")


def test_make_llm_requires_key_env(tmp_path, monkeypatch):
    from qfii.config import load_config
    p = tmp_path / "c.toml"
    p.write_text('[models.mid]\nprovider = "openai_compat"\nmodel = "m"\n'
                 'base_url = "http://x/v1"\napi_key_env = "QFII_TEST_KEY"\n')
    cfg = load_config(p)
    monkeypatch.delenv("QFII_TEST_KEY", raising=False)
    with pytest.raises(LLMError, match="QFII_TEST_KEY"):
        make_llm(cfg, "research", lambda s, u: "")
    monkeypatch.setenv("QFII_TEST_KEY", "secret")
    assert make_llm(cfg, "research", lambda s, u: "").model == "m"
