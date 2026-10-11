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
    cutoff: str | None = None     # minute cutoff, e.g. "2026-10-07T11:20" (overrides as_of)
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
# "abstain" is never offered to a model: the engine records it when a member
# produces no valid ballot, and it counts toward nothing.
BallotAction = Literal["invest", "replace", "pass", "request_info", "send_back", "propose",
                       "support", "abstain"]
Team = Literal["assessment", "research", "risk", "both"]


class Amendment(BaseModel):
    """A member's modified deal: the committee can rally around it instead of the
    Assessment proposal. Terms are fixed, so backing it means agreeing on the size."""

    size_usd: float = Field(gt=0)
    summary: str = Field(min_length=1, max_length=600)   # what changed vs. the proposal, and why
    holding_period_days: int | None = Field(default=None, gt=0, le=3650)
    stop_loss_pct: float | None = Field(default=None, gt=0, lt=100)    # exit if down this much
    take_profit_pct: float | None = Field(default=None, gt=0, le=1000)  # exit if up this much
    conditions: list[str] = Field(default_factory=list, max_length=5)   # e.g. review triggers

    @model_validator(mode="before")
    @classmethod
    def _drop_empty(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = {k: v for k, v in data.items()
                    if v not in ("", 0, None) or k in ("size_usd", "summary")}
        return data


class Ballot(BaseModel):
    """One member's argument and equal-weight vote for one round."""

    member: Member
    action: BallotAction
    argument: str = Field(min_length=1, max_length=3000)
    size_usd: float | None = Field(default=None, ge=0)
    replace_symbol: str | None = None
    target: Team | None = None
    request: str | None = Field(default=None, max_length=1500)
    note_to_self: str | None = Field(default=None, max_length=500)
    changed_because: str | None = Field(default=None, max_length=800)
    amendment: Amendment | None = None        # for propose
    amendment_id: str | None = None           # for support (and set on propose by the engine)

    @model_validator(mode="before")
    @classmethod
    def _drop_unused_fields(cls, data: Any) -> Any:
        """Models fill every field ("target": "", "size_usd": 0). Drop the ones the
        chosen action does not use, and empty strings, before validating."""
        if not isinstance(data, dict):
            return data
        data = {k: v for k, v in data.items()
                if not (isinstance(v, str) and not v.strip() and k != "argument")}
        action = data.get("action")
        all_optional = ("size_usd", "replace_symbol", "target", "request", "amendment",
                        "amendment_id")
        used = {"invest": ("size_usd",), "replace": ("size_usd", "replace_symbol"),
                "request_info": ("target", "request"), "send_back": ("target", "request"),
                "propose": ("amendment", "amendment_id"), "support": ("amendment_id",)}
        for key in set(all_optional) - set(used.get(action, ())):
            data.pop(key, None)
        if isinstance(data.get("amendment_id"), str):
            data["amendment_id"] = data["amendment_id"].strip().upper()
        return data

    @model_validator(mode="after")
    def _fields_match_action(self) -> Ballot:
        if self.action in ("invest", "replace") and not self.size_usd:
            raise ValueError(f"{self.action} needs a positive size_usd")
        if self.action == "replace" and not self.replace_symbol:
            raise ValueError("replace needs replace_symbol (the position to sell)")
        if self.action in ("request_info", "send_back") and not (self.target and self.request):
            raise ValueError(f"{self.action} needs target and request")
        if self.action == "propose" and self.amendment is None:
            raise ValueError("propose needs an amendment (size_usd, summary, optional terms)")
        if self.action == "support" and not self.amendment_id:
            raise ValueError("support needs amendment_id, e.g. \"A1\"")
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
    tax: dict[str, Any]          # TaxProfile + long_term_after_days; from the user's income
    investor: dict[str, Any]     # capital the user allocated, filing status


def ballot_reply_schema() -> dict[str, Any]:
    """JSON schema for what a member's LLM returns: a Ballot without `member`
    (the code signs ballots, so a member cannot vote as someone else)."""
    schema = Ballot.model_json_schema()
    schema["properties"].pop("member", None)
    actions = schema["properties"]["action"]["enum"]
    schema["properties"]["action"]["enum"] = [a for a in actions if a != "abstain"]
    schema["required"] = [r for r in schema.get("required", []) if r != "member"]
    return schema


# ---------------------------------------------------------------- DRAFT team reports
# Proposed formats for Ben to approve. Every claim cites evidence ids from the
# shared evidence store, so numbers travel by reference and are never retyped.

Severity = Literal["low", "medium", "high", "blocking"]


class Claim(BaseModel):
    """A cited statement. data_gap=True marks a statement about MISSING data
    (e.g. "no fundamentals available"), which has nothing to cite."""

    claim: str = Field(min_length=1, max_length=1200)
    evidence_ids: list[str] = Field(default_factory=list)
    data_gap: bool = False

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        # live models wrote {"question": ..., "answer": ...} instead of {"claim": ...}
        if isinstance(data, dict) and "claim" not in data:
            for alias in ("answer", "statement", "text", "finding"):
                if alias in data:
                    data = {**data, "claim": data[alias]}
                    break
        return data

    @model_validator(mode="after")
    def _cited_unless_gap(self) -> Claim:
        if not self.evidence_ids and not self.data_gap:
            raise ValueError("cite evidence_ids, or set data_gap=true if the claim is about "
                             "missing data")
        return self


class Counterargument(Claim):
    rebuttal: str | None = Field(default=None, max_length=1200)


class Proposal(BaseModel):
    """DRAFT. Assessment (TAs) -> committee: the 'research paper' for one stock."""

    symbol: str
    as_of: str
    thesis: str = Field(min_length=1, max_length=1200)
    arguments: list[Claim] = Field(min_length=1, max_length=10)
    counterarguments: list[Counterargument] = Field(min_length=1, max_length=10)
    feasibility: str = Field(max_length=800)
    uncertainties: list[str] = Field(default_factory=list, max_length=10)
    invalidation_conditions: list[str] = Field(min_length=1, max_length=10)
    expected_holding_period_days: int = Field(gt=0)
    suggested_position_pct: float = Field(ge=0, le=100)
    verdict: Literal["submit", "reject", "defer", "needs_evidence"]
    research_questions: list[str] = Field(default_factory=list, max_length=5)


class ScreenItem(BaseModel):
    """Assessment's call on one research report in the week replay."""

    symbol: str
    decision: Literal["advance", "hold", "dig", "reject"]
    reason: str = Field(min_length=1, max_length=800)
    questions: list[str] = Field(default_factory=list, max_length=4)   # for "dig"
    evidence_ids: list[str] = Field(default_factory=list)

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    @model_validator(mode="after")
    def _dig_needs_questions(self) -> ScreenItem:
        if self.decision == "dig" and not self.questions:
            raise ValueError(f"{self.symbol}: dig needs questions for Research")
        if self.decision != "dig":
            self.questions = []
        return self


class Screen(BaseModel):
    """Assessment reads every report at once: advance to the committee, hold,
    send Research to dig deeper, or reject."""

    items: list[ScreenItem] = Field(min_length=1)
    notes: str = Field(default="", max_length=1500)


class RiskFinding(BaseModel):
    issue: str = Field(min_length=1, max_length=800)
    severity: Severity
    evidence_ids: list[str] = Field(default_factory=list)
    data_gap: bool = False
    detail: str = Field(default="", max_length=1500)

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        # live models wrote a finding's text as "claim"
        if isinstance(data, dict) and "issue" not in data:
            for alias in ("claim", "finding", "title", "risk"):
                if alias in data:
                    data = {**data, "issue": data[alias]}
                    break
        return data

    @model_validator(mode="after")
    def _cited_unless_gap(self) -> RiskFinding:
        if not self.evidence_ids and not self.data_gap:
            raise ValueError("cite evidence_ids, or set data_gap=true for a missing-data finding")
        return self


class AssumptionCheck(BaseModel):
    assumption: str = Field(max_length=800)   # quoted from the proposal
    holds: bool | None                         # None: cannot be checked (data gap)
    evidence_ids: list[str] = Field(default_factory=list)
    data_gap: bool = False
    detail: str = Field(default="", max_length=1500)

    @model_validator(mode="after")
    def _cited_unless_gap(self) -> AssumptionCheck:
        if not self.evidence_ids and not self.data_gap:
            raise ValueError("cite evidence_ids, or set data_gap=true if it cannot be checked")
        return self


class RiskReport(BaseModel):
    """DRAFT. Risk & Validation -> committee, produced only when commissioned.
    Uses the same MCP tools as Research to check the proposal against real data."""

    symbol: str
    as_of: str
    answers: list[Claim] = Field(default_factory=list)   # to the committee's questions
    findings: list[RiskFinding] = Field(default_factory=list, max_length=20)
    assumption_checks: list[AssumptionCheck] = Field(default_factory=list, max_length=20)
    liquidity: str = Field(default="", max_length=1500)
    portfolio_impact: str = Field(default="", max_length=1500)  # weight, cash after, tax


# Machine-readable formats for every message in the system (docs/schemas/*.json).
MESSAGE_FORMATS: dict[str, type[BaseModel]] = {
    "ResearchRequest": ResearchRequest,
    "EvidencePacket": EvidencePacket,
    "Proposal": Proposal,
    "RiskReport": RiskReport,
    "Ballot": Ballot,
    "PortfolioSnapshot": PortfolioSnapshot,
}


class AssessmentAnswer(BaseModel):
    """Assessment -> committee, when the committee votes request_info -> assessment."""

    answers: list[Claim] = Field(min_length=1, max_length=10)
    revised_thesis: str | None = Field(default=None, max_length=1200)


def cited_ids(model: BaseModel) -> set[str]:
    """Every evidence id cited anywhere in a report."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, BaseModel):
            for name, field_value in value:
                if name == "evidence_ids":
                    found.update(field_value)
                else:
                    walk(field_value)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(model)
    return found
