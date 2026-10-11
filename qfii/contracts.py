"""Messages exchanged between Qfii agents (A2A data parts) and returned by tools (MCP).

Only the Research contracts are defined here. Proposal, RiskReport, committee
votes and the decision follow once Ben freezes the ADE spec.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

EvidenceKind = Literal["price", "calc", "filing", "macro", "news"]
Specialty = Literal["fundamental", "technical", "macro_news"]
QualityFlag = Literal[
    "stale_data",        # last data point older than research.stale_after_days before as_of
    "short_sample",      # fewer than research.min_observations data points
    "missing_values",    # gaps or non-positive prices in the series
    "synthetic_data",    # development data, not real market prices
    "no_fundamentals",   # no SEC fundamentals available as of the cutoff
    "no_macro",          # no macro series available as of the cutoff
    "no_news",           # news tools not connected yet
    "no_benchmark",      # benchmark comparison unavailable
]


def evidence_key(symbol: str | None, kind: str, as_of: str, payload: dict[str, Any],
                 params: dict[str, Any]) -> str:
    blob = json.dumps([symbol, kind, as_of, payload, params], sort_keys=True, default=str)
    return "ev_" + hashlib.sha256(blob.encode()).hexdigest()[:16]


class ResearchRequest(BaseModel):
    """What the Research agent is asked to look at. `as_of` is the information cutoff:
    no data after it may be used (no look-ahead)."""

    symbol: str
    as_of: date | None = None
    question: str | None = None
    requested_by: Literal["discovery", "assessment", "risk", "aggressive", "conservative",
                          "neutral", "user"] = "user"

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        v = v.strip().upper()
        if not v or not v.replace(".", "").replace("-", "").isalnum():
            raise ValueError(f"invalid symbol {v!r}")
        return v


class EvidenceItem(BaseModel):
    id: str
    symbol: str | None
    kind: EvidenceKind
    source: str
    as_of: str
    retrieved_at: str
    payload: dict[str, Any]
    params: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def make(cls, *, symbol: str | None, kind: EvidenceKind, source: str, as_of: str,
             retrieved_at: str, payload: dict[str, Any],
             params: dict[str, Any] | None = None) -> EvidenceItem:
        params = params or {}
        return cls(id=evidence_key(symbol, kind, as_of, payload, params), symbol=symbol,
                   kind=kind, source=source, as_of=as_of, retrieved_at=retrieved_at,
                   payload=payload, params=params)


class Observation(BaseModel):
    """One factual statement. Every observation must cite at least one evidence id."""

    specialty: Specialty
    text: str = Field(min_length=1, max_length=400)
    evidence_ids: list[str] = Field(min_length=1)


class ResearchNotes(BaseModel):
    """The JSON the Research LLM must return."""

    observations: list[Observation] = Field(min_length=1, max_length=30)
    gaps: list[str] = Field(default_factory=list, max_length=10)


class EvidencePacket(BaseModel):
    """Research -> Assessment. Facts only; no recommendation."""

    symbol: str
    as_of: str
    observations: list[Observation]
    evidence: list[EvidenceItem]
    quality_flags: list[QualityFlag]
    gaps: list[str]
    model: str
    created_at: str

    @model_validator(mode="after")
    def _citations_resolve(self) -> EvidencePacket:
        known = {e.id for e in self.evidence}
        for obs in self.observations:
            missing = [i for i in obs.evidence_ids if i not in known]
            if missing:
                raise ValueError(f"observation cites unknown evidence: {missing}")
        return self


# ---------------------------------------------------------------- committee

Member = Literal["aggressive", "conservative", "neutral"]
BallotAction = Literal["invest", "replace", "pass", "request_info", "send_back"]
Team = Literal["assessment", "risk", "both"]


class Ballot(BaseModel):
    """One member's argument and equal-weight vote for one round."""

    member: Member
    action: BallotAction
    argument: str = Field(min_length=1, max_length=3000)
    size_usd: float | None = Field(default=None, gt=0)
    replace_symbol: str | None = None
    target: Team | None = None
    request: str | None = Field(default=None, max_length=1500)
    note_to_self: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _fields_match_action(self) -> Ballot:
        if self.action in ("invest", "replace") and self.size_usd is None:
            raise ValueError(f"{self.action} needs size_usd")
        if self.action == "replace" and not self.replace_symbol:
            raise ValueError("replace needs replace_symbol (the position to sell)")
        if self.action in ("request_info", "send_back") and not (self.target and self.request):
            raise ValueError(f"{self.action} needs target and request")
        if self.action not in ("invest", "replace"):
            self.size_usd = None
        if self.action != "replace":
            self.replace_symbol = None
        if self.action not in ("request_info", "send_back"):
            self.target = None
            self.request = None
        if self.replace_symbol:
            self.replace_symbol = self.replace_symbol.strip().upper()
        return self


class Holding(BaseModel):
    symbol: str
    qty: float
    avg_cost: float
    last_price: float
    market_value_usd: float
    weight_pct: float
    unrealized_pnl_usd: float
    oldest_lot_days: int
    short_term_qty: float          # held < long_term_after_days: taxed at short-term rate
    long_term_qty: float
    est_tax_if_sold_usd: float     # on unrealized gains only; 0 if at a loss
    avg_daily_volume_usd: float    # 20-day average, for liquidity
    pct_of_daily_volume: float     # position value / avg daily dollar volume, in %


class PortfolioSnapshot(BaseModel):
    """Shared by Assessment, Risk and all three committee members (not Research)."""

    as_of: str
    cash_usd: float
    equity_usd: float
    cash_pct: float
    holdings: list[Holding]
    benchmark: str
    limits: dict[str, float]
    costs_bps: dict[str, float]
    tax: dict[str, float]


def ballot_reply_schema() -> dict[str, Any]:
    """JSON schema for what a member's LLM returns: a Ballot without `member`
    (the code signs ballots, so a member cannot vote as someone else)."""
    schema = Ballot.model_json_schema()
    schema["properties"].pop("member", None)
    schema["required"] = [r for r in schema.get("required", []) if r != "member"]
    return schema
