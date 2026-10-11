from datetime import date

import pytest

from qfii.db import connect, init_db
from qfii.portfolio import snapshot
from qfii.tools.market import load_prices

AS_OF = date(2026, 10, 9)


def test_empty_portfolio(cfg):
    conn = connect(":memory:")
    init_db(conn, 100_000)
    snap = snapshot(conn, cfg, AS_OF)
    assert (snap.cash_usd, snap.equity_usd, snap.cash_pct, snap.holdings) == (100000, 100000,
                                                                              100, [])
    assert snap.benchmark == "SPY" and snap.limits["max_order_usd"] == 10000


def test_holding_lots_tax_and_liquidity(cfg):
    conn = connect(":memory:")
    init_db(conn, 100_000)
    price = load_prices("synthetic", cfg.csv_dir, "MSFT", AS_OF)[-1].close
    # one long-term lot at half price, one short-term lot at the current price
    conn.execute("INSERT INTO lots (symbol, qty, qty_open, cost_per_share, opened_at) "
                 "VALUES ('MSFT', 10, 10, ?, '2025-01-02')", (price / 2,))
    conn.execute("INSERT INTO lots (symbol, qty, qty_open, cost_per_share, opened_at) "
                 "VALUES ('MSFT', 5, 5, ?, '2026-09-01')", (price,))
    conn.execute("INSERT INTO cash_ledger (amount_cents, reason, created_at) "
                 "VALUES (?, 'buy', '2026-09-01')", (-round((10 * price / 2 + 5 * price) * 100),))
    snap = snapshot(conn, cfg, AS_OF)
    h = snap.holdings[0]
    assert h.symbol == "MSFT" and h.qty == 15
    assert (h.long_term_qty, h.short_term_qty) == (10, 5)
    # long-term gain 10 * price/2 taxed at 15%; short-term lot has no gain
    assert h.est_tax_if_sold_usd == pytest.approx(10 * price / 2 * 0.15, abs=0.01)
    assert h.oldest_lot_days == (AS_OF - date(2025, 1, 2)).days
    assert h.market_value_usd == pytest.approx(15 * price, abs=0.01)
    assert snap.equity_usd == pytest.approx(snap.cash_usd + h.market_value_usd, abs=0.01)
    assert 0 < h.pct_of_daily_volume < 100
