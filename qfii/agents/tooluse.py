"""Let a team agent research on its own: call MCP tools, read results, call more,
then answer. Used when the committee asks Assessment or Risk & Validation a
question.

Each turn the model replies with ONE JSON object, either
    {"tool_calls": [{"tool": "<name>", "arguments": {...}}, ...]}
or
    {"final": {<the report>}}
Every tool result becomes an EvidenceItem with its own id, so the final report
can cite what the agent looked up or calculated. as_of is forced to the paper's
cutoff on every call, so an agent can never look past it. Any symbol is allowed
(peer comparisons). The number of turns is capped.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..contracts import EvidenceItem, cited_ids
from ..llm import LLM, LLMError, failed_call, parse_json_object
from ..tools.client import ToolBox, ToolError

T = TypeVar("T", bound=BaseModel)
RESULT_CHARS = 6000          # cap per tool result shown to the model
MAX_CALLS_PER_TURN = 4
# never offered: superseded by the SEC tools, or raw series better read via indicators
HIDDEN_TOOLS = {"get_fundamentals"}

PROTOCOL = """
## Tools

You may research before answering. Each turn reply with ONE JSON object:
  {"tool_calls": [{"tool": "<name>", "arguments": {...}}]}   (up to 4 calls), or
  {"final": {...your report...}}
Tool results come back as `tool_results`, each with an `evidence_id` you can cite.
`as_of` is fixed to the paper's information cutoff; you need not pass it. Other
symbols are allowed (e.g. a peer). Never compute numbers yourself: use the
`calculate` tool (e.g. market cap = price x diluted shares; P/E = market cap /
net income) and cite its result. When you have what you need, give "final".
"""


def _trim(value: Any, limit: int = RESULT_CHARS) -> Any:
    text = json.dumps(value, default=str)
    if len(text) <= limit:
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(v, list) and len(v) > 30:   # long series: keep the latest
                v = v[-30:]
            out[k] = v
        text = json.dumps(out, default=str)
        if len(text) <= limit:
            return out
    return {"truncated": text[:limit]}


def _evidence_for(tool: str, args: dict[str, Any], result: dict[str, Any],
                  as_of: str) -> EvidenceItem:
    kind = ("filing" if tool.startswith(("get_sec_fundamentals", "search_sec"))
            else "news" if tool == "get_sec_news"
            else "macro" if tool == "get_macro_snapshot"
            else "price" if tool == "get_price_history" else "calc")
    label = result.get("label") or f"{tool}({', '.join(f'{k}={v}' for k, v in args.items())})"
    return EvidenceItem.make(symbol=args.get("symbol"), kind=kind, source=f"mcp:{tool}",
                             as_of=as_of, retrieved_at=datetime.now(UTC).isoformat(),
                             payload={"name": tool, "label": label, "value": result.get("value"),
                                      "result": _trim(result)}, params=args)


async def research_then_answer(llm: LLM, system: str, payload: dict[str, Any],
                               model: type[T], tools: ToolBox, as_of: str,
                               known_ids: set[str], calls: list[dict[str, Any]],
                               max_turns: int = 6, fix: Any = None
                               ) -> tuple[T, list[EvidenceItem], list[dict[str, Any]]]:
    """Run the tool loop. Returns (report, evidence gathered, trace of tool calls)."""
    from .structured import AgentFailed

    catalog = [t for t in await tools.describe() if t["name"] not in HIDDEN_TOOLS]
    names = {t["name"] for t in catalog}
    gathered: list[EvidenceItem] = []
    trace: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    feedback = None
    for turn in range(1, max_turns + 1):
        last = turn == max_turns
        body = {**payload, "available_tools": catalog, "tool_results": results,
                "turns_left": max_turns - turn}
        if last:
            body["instruction"] = "No tool turns left: reply with {\"final\": ...} now."
        if feedback:
            body["your_previous_reply_was_rejected_because"] = feedback
        try:
            reply = await llm.complete(system + PROTOCOL,
                                       json.dumps(body, separators=(",", ":"), default=str))
        except LLMError as exc:
            calls.append(failed_call(llm, str(exc)))
            feedback = str(exc)
            continue
        call = reply.record()
        calls.append(call)
        try:
            data = parse_json_object(reply.text)
        except ValueError as exc:
            call.update(ok=False, error=f"invalid reply: {exc}"[:500])
            feedback = f"reply was not one JSON object: {exc}"
            continue

        if "final" in data or "tool_calls" not in data:
            final = data.get("final", data)
            try:
                if fix:
                    final = fix(final)
                obj = model.model_validate(final)
                unknown = sorted(cited_ids(obj) - known_ids - {e.id for e in gathered})
                if unknown:
                    raise ValueError(f"cited evidence ids that do not exist: {unknown[:5]}")
                return obj, gathered, trace
            except (ValueError, ValidationError) as exc:
                call.update(ok=False, error=f"invalid final: {exc}"[:500])
                feedback = str(exc)[:600]
                continue

        requested = data.get("tool_calls")
        if not isinstance(requested, list) or not requested:
            feedback = 'reply with {"tool_calls": [...]} or {"final": {...}}'
            continue
        feedback = None
        for req in requested[:MAX_CALLS_PER_TURN]:
            name = str((req or {}).get("tool", ""))
            args = dict((req or {}).get("arguments") or {})
            if name not in names:
                results.append({"tool": name, "error": f"unknown tool; choose from "
                                                       f"{sorted(names)}"})
                continue
            if "as_of" in next(t["arguments"] for t in catalog if t["name"] == name):
                args["as_of"] = as_of                      # never past the cutoff
            if isinstance(args.get("symbol"), str):
                args["symbol"] = args["symbol"].upper()
            try:
                result = await tools.call(name, **args)
            except (ToolError, TypeError, ValueError) as exc:
                results.append({"tool": name, "arguments": args, "error": str(exc)[:400]})
                trace.append({"tool": name, "arguments": args, "ok": False,
                              "error": str(exc)[:200]})
                continue
            ev = _evidence_for(name, args, result, as_of)
            gathered.append(ev)
            results.append({"tool": name, "arguments": args, "evidence_id": ev.id,
                            "result": ev.payload["result"]})
            trace.append({"tool": name, "arguments": args, "ok": True, "evidence_id": ev.id,
                          "value": result.get("value")})
    raise AgentFailed(f"no valid {model.__name__} after {max_turns} turns: {feedback}", calls)
