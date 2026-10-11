"""US federal tax rates on investment gains, from the user's income and filing status.

Deterministic on purpose: the LLMs read the resulting rates from the portfolio
snapshot; they never guess a bracket. If the user gives no income, a stated
default bracket is used and flagged as an assumption so every agent can see it.

Figures: tax year 2026, IRS Rev. Proc. 2025-32. VERIFY before relying on them.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

FILING_STATUSES = ("single", "married_joint")

# (upper bound of taxable income, marginal ordinary rate %)
ORDINARY = {
    "single": [(12_400, 10), (50_400, 12), (105_700, 22), (201_775, 24), (256_225, 32),
               (640_600, 35), (math.inf, 37)],
    "married_joint": [(24_800, 10), (100_800, 12), (211_400, 22), (403_550, 24),
                      (512_450, 32), (768_700, 35), (math.inf, 37)],
}
STANDARD_DEDUCTION = {"single": 16_100, "married_joint": 32_200}
# long-term capital gains: (upper bound of taxable income, rate %)
LTCG = {
    "single": [(49_450, 0), (545_500, 15), (math.inf, 20)],
    "married_joint": [(98_900, 0), (613_700, 15), (math.inf, 20)],
}
NIIT_PCT = 3.8  # net investment income tax above these (not inflation-indexed)
NIIT_THRESHOLD = {"single": 200_000, "married_joint": 250_000}

DEFAULT_ASSUMPTION = {"ordinary_pct": 24.0, "ltcg_pct": 15.0}


@dataclass(frozen=True)
class TaxProfile:
    filing_status: str
    income_usd: float | None
    taxable_income_usd: float | None
    ordinary_bracket_pct: float
    ltcg_pct: float
    niit_pct: float
    state_pct: float
    short_term_pct: float     # what a gain on a lot held < 1 year costs
    long_term_pct: float
    assumed: bool
    basis: str

    def to_dict(self) -> dict:
        return asdict(self)


def _rate(table: list[tuple[float, float]], taxable: float) -> float:
    return next(rate for bound, rate in table if taxable <= bound)


def tax_profile(income_usd: float | None, filing_status: str = "single",
                state_pct: float = 0.0) -> TaxProfile:
    if filing_status not in FILING_STATUSES:
        raise ValueError(f"filing_status must be one of {FILING_STATUSES}")
    if not 0 <= state_pct <= 20:
        raise ValueError("state_pct must be between 0 and 20")
    if income_usd is None:
        ordinary, ltcg, niit = (DEFAULT_ASSUMPTION["ordinary_pct"],
                                DEFAULT_ASSUMPTION["ltcg_pct"], 0.0)
        return TaxProfile(filing_status, None, None, ordinary, ltcg, niit, state_pct,
                          ordinary + state_pct, ltcg + state_pct, True,
                          "income not provided: assumed the 24% bracket and 15% long-term rate")
    if income_usd < 0:
        raise ValueError("income must be >= 0")
    taxable = max(0.0, income_usd - STANDARD_DEDUCTION[filing_status])
    ordinary = _rate(ORDINARY[filing_status], taxable)
    ltcg = _rate(LTCG[filing_status], taxable)
    niit = NIIT_PCT if income_usd > NIIT_THRESHOLD[filing_status] else 0.0
    return TaxProfile(
        filing_status, income_usd, taxable, ordinary, ltcg, niit, state_pct,
        ordinary + niit + state_pct, ltcg + niit + state_pct, False,
        f"2026 federal brackets ({filing_status}, standard deduction), "
        f"marginal rate at ${taxable:,.0f} taxable income",
    )
