"""The user's inputs: how much capital the committee may allocate, and the tax
situation that sets short- and long-term rates. Set with `qfii setup`."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .db import utcnow
from .tax import TaxProfile, tax_profile


class SetupError(ValueError):
    pass


@dataclass(frozen=True)
class InvestorProfile:
    capital_usd: float
    income_usd: float | None
    filing_status: str
    state_tax_pct: float

    @property
    def tax(self) -> TaxProfile:
        return tax_profile(self.income_usd, self.filing_status, self.state_tax_pct)


def get_profile(conn: sqlite3.Connection) -> InvestorProfile | None:
    row = conn.execute("SELECT capital_usd, income_usd, filing_status, state_tax_pct "
                       "FROM investor_profile WHERE id = 1").fetchone()
    return InvestorProfile(*row) if row else None


def set_profile(conn: sqlite3.Connection, capital_usd: float, income_usd: float | None = None,
                filing_status: str = "single", state_tax_pct: float = 0.0) -> InvestorProfile:
    """Record the profile. Capital becomes the cash balance, so it can only be set
    before the first trade (afterwards cash comes from the ledger)."""
    if capital_usd <= 0:
        raise SetupError("capital must be positive")
    tax_profile(income_usd, filing_status, state_tax_pct)  # validates inputs
    traded = conn.execute("SELECT COUNT(*) FROM cash_ledger WHERE reason <> 'deposit'"
                          ).fetchone()[0]
    current = get_profile(conn)
    if traded and (current is None or current.capital_usd != capital_usd):
        raise SetupError("trades exist: capital can no longer be changed (tax details can)")
    with conn:
        if not traded:
            conn.execute("DELETE FROM cash_ledger WHERE reason = 'deposit'")
            conn.execute("INSERT INTO cash_ledger (amount_cents, reason, created_at) "
                         "VALUES (?, 'deposit', ?)", (round(capital_usd * 100), utcnow()))
        conn.execute(
            "INSERT INTO investor_profile (id, capital_usd, income_usd, filing_status, "
            "state_tax_pct, updated_at) VALUES (1, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "capital_usd = excluded.capital_usd, income_usd = excluded.income_usd, "
            "filing_status = excluded.filing_status, state_tax_pct = excluded.state_tax_pct, "
            "updated_at = excluded.updated_at",
            (capital_usd, income_usd, filing_status, state_tax_pct, utcnow()))
    return get_profile(conn)
