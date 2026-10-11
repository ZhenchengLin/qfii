"""Portfolio snapshot shared with Assessment, Risk and the committee.

Built from the cash ledger and open tax lots, priced with the same data source
the tools use, as of the same cutoff date.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from .config import Config
from .contracts import Holding, PortfolioSnapshot
from .db import cash_balance_cents
from .investor import get_profile
from .tax import tax_profile
from .tools.market import load_prices

ADV_WINDOW = 20


def snapshot(conn: sqlite3.Connection, cfg: Config, as_of: date) -> PortfolioSnapshot:
    cash = cash_balance_cents(conn) / 100
    pf, lim = cfg.portfolio, cfg.limits
    profile = get_profile(conn)
    tax = profile.tax if profile else tax_profile(None)
    lots_by_symbol: dict[str, list[tuple[float, float, date]]] = {}
    for symbol, qty, cost, opened in conn.execute(
            "SELECT symbol, qty_open, cost_per_share, opened_at FROM lots "
            "WHERE qty_open > 0 ORDER BY opened_at"):
        lots_by_symbol.setdefault(symbol, []).append((qty, cost, date.fromisoformat(opened[:10])))

    rows = []
    for symbol, lots in sorted(lots_by_symbol.items()):
        bars = load_prices(cfg.data_source, cfg.csv_dir, symbol, as_of)
        if not bars:
            raise RuntimeError(f"no price for held position {symbol} as of {as_of}")
        price = bars[-1].close
        tail = bars[-ADV_WINDOW:]
        adv = sum(b.close * b.volume for b in tail) / len(tail)
        qty = sum(q for q, _, _ in lots)
        cost_basis = sum(q * c for q, c, _ in lots)
        short_q = sum(q for q, _, d in lots if (as_of - d).days < pf.long_term_after_days)
        est_tax = 0.0
        for q, c, d in lots:
            gain = (price - c) * q
            rate = (tax.short_term_pct if (as_of - d).days < pf.long_term_after_days
                    else tax.long_term_pct)
            est_tax += max(gain, 0.0) * rate / 100
        rows.append((symbol, qty, cost_basis, price, adv, short_q, est_tax,
                     max((as_of - d).days for _, _, d in lots)))

    equity = cash + sum(qty * price for _, qty, _, price, *_ in rows)
    holdings = [
        Holding(
            symbol=symbol, qty=qty, avg_cost=round(cost_basis / qty, 4), last_price=price,
            market_value_usd=round(qty * price, 2),
            weight_pct=round(qty * price / equity * 100, 2) if equity else 0.0,
            unrealized_pnl_usd=round(qty * price - cost_basis, 2),
            oldest_lot_days=days, short_term_qty=short_q, long_term_qty=qty - short_q,
            est_tax_if_sold_usd=round(est, 2), avg_daily_volume_usd=round(adv, 2),
            pct_of_daily_volume=round(qty * price / adv * 100, 4) if adv else 100.0,
        )
        for symbol, qty, cost_basis, price, adv, short_q, est, days in rows
    ]
    return PortfolioSnapshot(
        as_of=as_of.isoformat(), cash_usd=round(cash, 2), equity_usd=round(equity, 2),
        cash_pct=round(cash / equity * 100, 2) if equity else 100.0, holdings=holdings,
        benchmark=pf.benchmark,
        limits={"max_position_pct": lim.max_position_pct, "max_order_usd": lim.max_order_usd,
                "min_cash_pct": lim.min_cash_pct, "daily_order_cap": lim.daily_order_cap},
        costs_bps={"commission": pf.commission_bps, "slippage": pf.slippage_bps},
        tax={**tax.to_dict(), "long_term_after_days": pf.long_term_after_days},
        investor={"capital_usd": profile.capital_usd if profile else None,
                  "filing_status": tax.filing_status,
                  "income_provided": tax.income_usd is not None,
                  "profile_set": profile is not None},
    )


def max_new_order_usd(snap: PortfolioSnapshot | dict) -> float:
    """Largest buy the hard limits allow right now: max order size, max position
    weight, and cash above the minimum reserve. Code computes it; agents see it."""
    s = snap if isinstance(snap, dict) else snap.model_dump()
    lim, equity = s["limits"], s["equity_usd"]
    free_cash = s["cash_usd"] - equity * lim["min_cash_pct"] / 100
    return round(max(0.0, min(lim["max_order_usd"], equity * lim["max_position_pct"] / 100,
                              free_cash)), 2)
