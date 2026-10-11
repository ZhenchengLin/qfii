"""Company fundamentals from local SEC EDGAR companyfacts files.

Look-ahead safe: only facts whose `filed` date is on or before as_of are used,
so a 10-K filed in November is invisible to an October analysis.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .market import DataError

# metric -> candidate us-gaap concepts (companies use different tags)
FLOW = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "SalesRevenueNet"],
    "net_income": ["NetIncomeLoss"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "eps_diluted": ["EarningsPerShareDiluted"],
}
INSTANT = {
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "stockholders_equity": ["StockholdersEquity"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "long_term_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
}


def load_facts(fund_dir: Path, symbol: str) -> dict:
    path = fund_dir / f"{symbol}.json"
    if not path.exists():
        raise DataError(f"no fundamentals file for {symbol} (run `qfii data fetch "
                        f"--fundamentals` with data.sec_user_agent set)")
    return json.loads(path.read_text())


def _rows(facts: dict, concept: str) -> list[dict]:
    units = facts.get("facts", {}).get("us-gaap", {}).get(concept, {}).get("units", {})
    return units.get("USD") or units.get("USD/shares") or []


def _known(rows: list[dict], as_of: str) -> list[dict]:
    """Facts public by as_of; for each period end keep the latest filing (restatements)."""
    by_end: dict[tuple, dict] = {}
    for r in rows:
        if r.get("filed", "9999") > as_of:
            continue
        key = (r.get("start"), r["end"])
        if key not in by_end or r["filed"] > by_end[key]["filed"]:
            by_end[key] = r
    return sorted(by_end.values(), key=lambda r: r["end"])


def _annual(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if "start" not in r:
            continue
        days = (date.fromisoformat(r["end"]) - date.fromisoformat(r["start"])).days
        if 350 <= days <= 380 and r.get("form", "").startswith("10-K"):
            out.append(r)
    return out


def _pick(facts: dict, concepts: list[str], as_of: str, annual: bool) -> tuple[str, list[dict]]:
    for concept in concepts:
        rows = _known(_rows(facts, concept), as_of)
        if annual:
            rows = _annual(rows)
        if rows:
            return concept, rows
    return "", []


def fundamentals(facts: dict, as_of: date) -> dict:
    cutoff = as_of.isoformat()
    metrics: dict[str, dict] = {}
    for name, concepts in FLOW.items():
        concept, rows = _pick(facts, concepts, cutoff, annual=True)
        if rows:
            last = rows[-1]
            metrics[name] = {"value": last["val"], "period_end": last["end"],
                             "filed": last["filed"], "form": last.get("form"), "concept": concept,
                             "prior_year_value": rows[-2]["val"] if len(rows) > 1 else None}
    for name, concepts in INSTANT.items():
        concept, rows = _pick(facts, concepts, cutoff, annual=False)
        if rows:
            last = rows[-1]
            metrics[name] = {"value": last["val"], "period_end": last["end"],
                             "filed": last["filed"], "form": last.get("form"), "concept": concept}

    def v(name: str) -> float | None:
        return metrics.get(name, {}).get("value")

    derived = {}
    rev = metrics.get("revenue")
    if rev and rev.get("prior_year_value"):
        derived["revenue_growth_yoy"] = round(rev["value"] / rev["prior_year_value"] - 1, 6)
    if v("revenue") and v("net_income") is not None:
        derived["net_margin"] = round(v("net_income") / v("revenue"), 6)
    if v("stockholders_equity") and v("long_term_debt") is not None:
        derived["debt_to_equity"] = round(v("long_term_debt") / v("stockholders_equity"), 6)
    if v("total_assets") and v("total_liabilities") is not None:
        derived["liabilities_to_assets"] = round(v("total_liabilities") / v("total_assets"), 6)
    return {"available": bool(metrics), "entity": facts.get("entityName"),
            "metrics": metrics, "derived": derived}
