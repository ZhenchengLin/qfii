"""Deterministic stand-in for the Research LLM, so the agent runs with no API key.

It reads the same JSON prompt a real model gets and writes plain factual
sentences, citing the evidence ids it was given.
"""

from __future__ import annotations

import json

_LABELS = {
    "last_close": "Last close was {v:.2f} on {date}",
    "return_1m": "1-month return was {pct}",
    "return_3m": "3-month return was {pct}",
    "return_12m": "12-month return was {pct}",
    "sma_50": "50-day simple moving average was {v:.2f}",
    "sma_200": "200-day simple moving average was {v:.2f}",
    "volatility_3m_ann": "3-month annualized volatility was {pct}",
    "rsi_14": "14-day RSI was {v:.1f}",
    "max_drawdown_12m": "Maximum 12-month drawdown was {pct}",
}


def fake_research_responder(system: str, user: str) -> str:
    data = json.loads(user)
    observations = []
    for fact in data["facts"]:
        template = _LABELS.get(fact["name"])
        if template is None:
            continue
        v = fact["value"]
        text = template.format(v=v, pct=f"{v * 100:.2f}%", date=fact.get("date", ""))
        observations.append({"specialty": "technical", "text": f"{text}.",
                             "evidence_ids": [fact["evidence_id"]]})
    gaps = [f"quality flag: {f}" for f in data["quality_flags"]]
    return json.dumps({"observations": observations[:12], "gaps": gaps})
