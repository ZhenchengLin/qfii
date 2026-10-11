"""Committee members backed by an LLM. Same protocol, different persona and
model tier, and each sees only its own memory."""

from __future__ import annotations

import json
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


def system_prompt(member: str) -> str:
    return (_PROMPTS.joinpath(f"{member}.md").read_text() + "\n"
            + _PROMPTS.joinpath("protocol.md").read_text())


class LLMMember:
    def __init__(self, name: str, llm: LLM):
        if name not in COMMITTEE:
            raise ValueError(f"unknown committee member {name!r}")
        self.name = name
        self.llm = llm
        self.system = system_prompt(name)
        self.calls: list[dict[str, Any]] = []

    async def deliberate(self, view: DebateView) -> Ballot:
        user = json.dumps(view.as_prompt_dict(), separators=(",", ":"), default=str)
        try:
            result = await self.llm.complete(self.system, user, BALLOT_SCHEMA)
        except LLMError as exc:
            self.calls.append(failed_call(self.llm, str(exc)))
            raise
        call = result.record()
        self.calls.append(call)
        try:
            data = parse_json_object(result.text)
            data["member"] = self.name  # a member cannot vote as someone else
            return Ballot.model_validate(data)
        except (ValueError, ValidationError) as exc:
            call.update(ok=False, error=f"invalid ballot: {exc}"[:500])
            raise ValueError(f"invalid ballot from {self.name}: {exc}") from exc


def build_members(cfg: Config) -> list[LLMMember]:
    return [LLMMember(name, make_llm(cfg, name, fake_member_responder(name)))
            for name in COMMITTEE]
