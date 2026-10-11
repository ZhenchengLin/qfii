"""Scripted Assessment and Risk replies so the full pipeline runs offline."""

from __future__ import annotations

import json


def _by_name(facts: list[dict], *labels: str) -> list[dict]:
    hits = [f for f in facts if any(lbl in (f.get("label") or "") for lbl in labels)]
    return hits or facts[:1]


def fake_assessment_responder(system: str, user: str) -> str:
    data = json.loads(user)
    facts = data["facts"]
    if "committee_questions" in data:
        f = facts[0]
        return json.dumps({"answers": [{"claim": f"{f['label']} was {f['value']}.",
                                        "evidence_ids": [f["id"]]}]})
    up = _by_name(facts, "12-month price return", "3-month price return")[0]
    vol = _by_name(facts, "annualized volatility")[0]
    dd = _by_name(facts, "drawdown")[0]
    return json.dumps({
        "thesis": f"{data['symbol']} has positive momentum; size it modestly given volatility.",
        "arguments": [{"claim": f"{up['label']} was {up['value']}.", "evidence_ids": [up["id"]]}],
        "counterarguments": [{"claim": f"{vol['label']} was {vol['value']}.",
                              "evidence_ids": [vol["id"]],
                              "rebuttal": "A small position limits the impact."}],
        "feasibility": "Fits within the order and position limits.",
        "uncertainties": ["fundamentals and news are not available"],
        "invalidation_conditions": [f"a drawdown worse than {dd['value']}"],
        "expected_holding_period_days": 180,
        "suggested_position_pct": 5,
        "verdict": "submit",
    })


def fake_risk_responder(system: str, user: str) -> str:
    data = json.loads(user)
    facts = data["facts"]
    vol = _by_name(facts, "annualized volatility")[0]
    impact = _by_name(facts, "order pct of avg daily volume")[0]
    return json.dumps({
        "answers": [{"claim": f"{vol['label']} was {vol['value']}.", "evidence_ids": [vol["id"]]}
                    for _ in data["committee_questions"][:1]],
        "findings": [{"issue": "High volatility", "severity": "medium",
                      "evidence_ids": [vol["id"]], "detail": "Size accordingly."}],
        "assumption_checks": [{"assumption": "position is liquid", "holds": True,
                               "evidence_ids": [impact["id"]]}],
        "liquidity": f"{impact['label']}: {impact['value']}",
        "portfolio_impact": "Within limits.",
    })
