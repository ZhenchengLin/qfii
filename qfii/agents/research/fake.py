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
    "excess_return_3m": "3-month return minus the benchmark's was {pct}",
    "excess_return_12m": "12-month return minus the benchmark's was {pct}",
    "beta_12m": "12-month beta to the benchmark was {v:.2f}",
    "correlation_12m": "12-month correlation with the benchmark was {v:.2f}",
    "revenue_growth_yoy": "Annual revenue growth was {pct}",
    "net_margin": "Net margin was {pct}",
    "debt_to_equity": "Long-term debt to equity was {v:.2f}",
    "liabilities_to_assets": "Liabilities to assets was {v:.2f}",
    "eps_diluted": "Annual diluted EPS was ${v:.2f}",
    "avg_daily_dollar_volume_20d": "Average daily dollar volume over 20 days was ${v:,.0f}",
}
_MONEY = {"revenue", "net_income", "operating_cash_flow", "total_assets", "total_liabilities",
          "stockholders_equity", "cash", "long_term_debt"}


def fake_research_responder(system: str, user: str) -> str:
    data = json.loads(user)
    observations = []
    for fact in data["facts"]:
        name, v = fact["name"], fact["value"]
        if name in _LABELS:
            text = _LABELS[name].format(v=v, pct=f"{v * 100:.2f}%", date=fact.get("date", ""))
        elif name in _MONEY:
            text = (f"{name.replace('_', ' ').capitalize()} was ${v / 1e9:,.2f}B "
                    f"(period ending {fact.get('period_end')})")
        elif name == "8k_event":
            text = f"{fact['label']}: {v}"
        elif name == "filing_passage":
            text = f"{fact['label']}: {str(v)[:160]}"
        elif "observation_date" in fact:
            text = f"{fact['label']} was {v} on {fact['observation_date']}"
        elif fact.get("label"):
            text = f"{fact['label']} was {v}"
        else:
            continue
        observations.append({"specialty": fact.get("specialty", "technical"),
                             "text": f"{text}.", "evidence_ids": [fact["evidence_id"]]})
    gaps = [f"quality flag: {f}" for f in data["quality_flags"]]
    return json.dumps({"observations": observations[:30], "gaps": gaps})
