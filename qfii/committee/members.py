"""Committee members backed by an LLM. Same protocol, different persona and
model tier, and each sees only its own memory."""

from __future__ import annotations

import json
import time
from importlib import resources
from typing import Any

from pydantic import ValidationError

from ..config import COMMITTEE, Config
from ..contracts import Ballot, ballot_reply_schema
from ..llm import LLM, LLMError, failed_call, make_llm, parse_json_object
from .engine import DebateView
from .fake import fake_member_responder

_PROMPTS = resources.files("qfii.committee").joinpath("prompts")
BALLOT_SCHEMA = ballot_reply_schema()


MAX_ATTEMPTS = 2
PROTOCOL = _PROMPTS.joinpath("protocol.md").read_text()
_YOU = "\n\n## You\n"


def member_prompt(view: DebateView, persona: str, feedback: str | None = None) -> str:
    """Shared debate state first, the member's persona and private memory last, so
    a local model can reuse the cached prompt prefix across all three members."""
    private = view.private_dict()
    if feedback:
        private["your_previous_reply_was_rejected_because"] = feedback
    return (json.dumps(view.shared_dict(), separators=(",", ":"), default=str) + _YOU
            + persona + "\n" + json.dumps(private, separators=(",", ":"), default=str))


def read_member_prompt(user: str) -> dict[str, Any]:
    """Inverse of member_prompt, for scripted models."""
    shared, _, rest = user.partition(_YOU)
    return {**json.loads(shared), **json.loads(rest[rest.rindex("\n{") + 1:])}


class LLMMember:
    def __init__(self, name: str, llm: LLM):
        if name not in COMMITTEE:
            raise ValueError(f"unknown committee member {name!r}")
        self.name = name
        self.llm = llm
        self.system = PROTOCOL
        self.persona = _PROMPTS.joinpath(f"{name}.md").read_text()
        self.calls: list[dict[str, Any]] = []
        self.ballot_log: list[dict[str, Any]] = []   # per ballot: round, attempts, valid

    async def deliberate(self, view: DebateView) -> Ballot:
        feedback = None
        entry: dict[str, Any] = {"round": view.round, "attempts": [], "valid": False}
        self.ballot_log.append(entry)
        for _ in range(MAX_ATTEMPTS):
            user = member_prompt(view, self.persona, feedback)
            started = time.monotonic()
            try:
                result = await self.llm.complete(self.system, user, BALLOT_SCHEMA)
            except LLMError as exc:
                call = failed_call(self.llm, str(exc))
                call["seconds"] = round(time.monotonic() - started, 2)
                self.calls.append(call)
                entry["attempts"].append(call)
                feedback = str(exc)
                continue
            call = result.record()
            call["seconds"] = round(time.monotonic() - started, 2)
            self.calls.append(call)
            entry["attempts"].append(call)
            try:
                data = parse_json_object(result.text)
                data["member"] = self.name  # a member cannot vote as someone else
                ballot = Ballot.model_validate(data)
                cap = view.rules.get("largest_allowed_order_usd")
                if cap is not None and ballot.size_usd and ballot.size_usd > cap:
                    raise ValueError(f"size_usd {ballot.size_usd:,.0f} exceeds "
                                     f"largest_allowed_order_usd {cap:,.0f}")
                entry["valid"] = True
                return ballot
            except (ValueError, ValidationError) as exc:
                call.update(ok=False, error=f"invalid ballot: {exc}"[:500])
                feedback = str(exc)[:500]
        raise ValueError(f"no valid ballot from {self.name} after {MAX_ATTEMPTS} attempts: "
                         f"{feedback}")


def build_members(cfg: Config) -> list[LLMMember]:
    return [LLMMember(name, make_llm(cfg, name, fake_member_responder(name)))
            for name in COMMITTEE]
