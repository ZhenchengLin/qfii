"""One LLM call that must return a validated, fully cited JSON object.

The model's reply is parsed, validated against a Pydantic model, and every cited
evidence id must exist. A bad reply is retried once with the reason attached;
after that the caller gets an AgentFailed with every attempt logged.
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..contracts import EvidenceItem, cited_ids
from ..llm import LLM, LLMError, failed_call, parse_json_object
from ..tools.indicators import LABELS

T = TypeVar("T", bound=BaseModel)
MAX_ATTEMPTS = 2


class AgentFailed(RuntimeError):
    def __init__(self, message: str, calls: list[dict[str, Any]]):
        super().__init__(message)
        self.calls = calls


def fact_rows(evidence: list[EvidenceItem]) -> list[dict[str, Any]]:
    """Compact citable facts for a prompt: id, label, value (no raw price series)."""
    rows = []
    for ev in evidence:
        p = ev.payload
        if ev.kind == "price":
            rows.append({"id": ev.id, "label": f"last close on {p.get('last_date')}",
                         "value": p.get("last_close")})
        elif ev.kind == "news":
            rows.append({"id": ev.id, "label": p.get("label"), "value": p.get("headline"),
                         "excerpt": (p.get("text") or "")[:700]})
        elif p.get("name") == "filing_passage":
            rows.append({"id": ev.id, "label": p.get("label"), "value": p.get("text", "")[:900]})
        elif ev.kind == "macro":
            rows.append({"id": ev.id, "label": p.get("label") or p.get("series"),
                         "value": p.get("value"), "date": p.get("observation_date"),
                         "value_12m_earlier": p.get("value_12m_earlier")})
        else:
            name = p.get("name", "")
            row = {"id": ev.id, "label": p.get("label") or LABELS.get(name)
                   or name.replace("_", " "), "value": p.get("value")}
            if p.get("period_end"):
                row["period_end"] = p["period_end"]
            rows.append(row)
    return rows


async def structured_call(llm: LLM, system: str, payload: dict[str, Any], model: type[T],
                          known_ids: set[str], calls: list[dict[str, Any]],
                          fix: Any = None) -> T:
    """fix(data) may set fields the code owns (symbol, as_of) before validation."""
    feedback = None
    for _ in range(MAX_ATTEMPTS):
        body = dict(payload)
        if feedback:
            body["your_previous_reply_was_rejected_because"] = feedback
        try:
            result = await llm.complete(system, json.dumps(body, separators=(",", ":"),
                                                           default=str),
                                        model.model_json_schema())
        except LLMError as exc:
            calls.append(failed_call(llm, str(exc)))
            feedback = str(exc)
            continue
        call = result.record()
        calls.append(call)
        try:
            data = parse_json_object(result.text)
            if fix:
                data = fix(data)
            obj = model.model_validate(data)
            unknown = sorted(cited_ids(obj) - known_ids)
            if unknown:
                raise ValueError(f"cited evidence ids that do not exist: {unknown[:5]}")
            return obj
        except (ValueError, ValidationError) as exc:
            call.update(ok=False, error=f"invalid reply: {exc}"[:500])
            feedback = str(exc)[:600]
    raise AgentFailed(f"no valid {model.__name__} after {MAX_ATTEMPTS} attempts: {feedback}",
                      calls)
