"""Scripted Assessment and Risk replies so the full pipeline runs offline."""

from __future__ import annotations

import json


def _by_name(facts: list[dict], *labels: str) -> list[dict]:
    hits = [f for f in facts if any(lbl in (f.get("label") or "") for lbl in labels)]
    return hits or facts[:1]


def fake_screen(data: dict) -> str:
    """Advance the strongest 12-month return; on a first look, send Research to dig
    on the weakest; hold the rest."""
    def ret(r):
        f = next((x for x in r["facts"] if "12-month price return" in (x.get("label") or "")),
                 None)
        return (f["value"] if f else 0.0), f
    ranked = sorted(data["reports"], key=lambda r: ret(r)[0], reverse=True)
    items = []
    for i, r in enumerate(ranked):
        value, fact = ret(r)
        ids = [fact["id"]] if fact else []
        if i == 0:
            items.append({"symbol": r["symbol"], "decision": "advance",
                          "reason": f"strongest 12-month return ({value})", "evidence_ids": ids})
        elif i == len(ranked) - 1 and not r.get("answering") and len(ranked) > 2:
            items.append({"symbol": r["symbol"], "decision": "dig",
                          "reason": "weakest; check the latest guidance",
                          "questions": ["revenue guidance next quarter"], "evidence_ids": ids})
        else:
            items.append({"symbol": r["symbol"], "decision": "hold",
                          "reason": "nothing decisive", "evidence_ids": ids})
    return json.dumps({"final": {"items": items, "notes": "scripted screen"}})


def fake_assessment_responder(system: str, user: str) -> str:
    data = json.loads(user)
    if "reports" in data:
        return fake_screen(data)
    facts = data["facts"]
    if "committee_questions" in data:
        results = data.get("tool_results") or []
        if "available_tools" in data and not results:  # research first: one calculation
            close = next((f for f in facts if "last close" in (f.get("label") or "")), facts[0])
            return json.dumps({"tool_calls": [{"tool": "calculate", "arguments": {
                "expression": f"{close['value']} * 2", "label": "twice the last close"}}]})
        done = [r for r in results if r.get("evidence_id")]
        f = facts[0]
        answers = [{"claim": f"{f['label']} was {f['value']}.", "evidence_ids": [f["id"]]}]
        if done:
            answers.append({"claim": f"Calculated {done[0]['result'].get('label')}: "
                                     f"{done[0]['result'].get('value')}.",
                            "evidence_ids": [done[0]["evidence_id"]]})
        return json.dumps({"final": {"answers": answers}})
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
