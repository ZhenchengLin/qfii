"""Qfii MCP tool server. Agents reach market data and calculations only through
these tools, so every number an agent cites came from deterministic code."""

from __future__ import annotations

from datetime import date

from mcp.server.mcpserver import MCPServer

from ..config import Config
from .fundamentals import fundamentals, load_facts
from .indicators import benchmark_stats, compute_all
from .macro import macro_snapshot
from .market import DataError, load_prices
from .quality import quality_flags


def build_tool_server(cfg: Config) -> MCPServer:
    server = MCPServer(
        "qfii-tools",
        instructions="Market data, technical indicators, benchmark comparison, macro "
                     "conditions, company fundamentals and data-quality checks for Qfii. All "
                     "tools take an as_of date (YYYY-MM-DD) and never return data that was "
                     "not public by then.",
    )

    def _bars(symbol: str, as_of: str):
        return load_prices(cfg.data_source, cfg.csv_dir, symbol.upper(), date.fromisoformat(as_of))

    @server.tool()
    def get_price_history(symbol: str, as_of: str, last_n: int = 260) -> dict:
        """Daily closes and volumes for `symbol` up to and including `as_of`."""
        bars = _bars(symbol, as_of)
        return {
            "symbol": symbol.upper(), "as_of": as_of, "source": cfg.data_source,
            "n": len(bars), "bars": [b.to_dict() for b in bars[-last_n:]],
        }

    @server.tool()
    def compute_indicators(symbol: str, as_of: str) -> dict:
        """Returns, moving averages, volatility, RSI and drawdown for `symbol` as of `as_of`."""
        bars = _bars(symbol, as_of)
        return {
            "symbol": symbol.upper(), "as_of": as_of, "source": cfg.data_source,
            "last_date": bars[-1].date if bars else None,
            "last_close": bars[-1].close if bars else None,
            "indicators": compute_all([b.close for b in bars]),
        }

    @server.tool()
    def check_data_quality(symbol: str, as_of: str) -> dict:
        """Quality flags (stale, short sample, missing values, synthetic) for `symbol`."""
        bars = _bars(symbol, as_of)
        flags = quality_flags(bars, date.fromisoformat(as_of), source=cfg.data_source,
                              min_observations=cfg.min_observations,
                              stale_after_days=cfg.stale_after_days)
        return {"symbol": symbol.upper(), "as_of": as_of, "n": len(bars), "flags": flags}

    @server.tool()
    def compare_to_benchmark(symbol: str, as_of: str) -> dict:
        """Excess return vs the benchmark (S&P 500 ETF by default) over 3 and 12 months,
        and 12-month beta and correlation."""
        bars = _bars(symbol, as_of)
        bench = _bars(cfg.portfolio.benchmark, as_of)
        stats = benchmark_stats([(b.date, b.close) for b in bars],
                                [(b.date, b.close) for b in bench])
        return {"symbol": symbol.upper(), "benchmark": cfg.portfolio.benchmark, "as_of": as_of,
                "source": cfg.data_source, "stats": stats}

    @server.tool()
    def get_macro_snapshot(as_of: str) -> dict:
        """Latest public value (respecting publication lag) and the values 3 and 12 months
        earlier for rates, inflation, unemployment, credit spreads and volatility."""
        return {"as_of": as_of, "source": "FRED",
                "series": macro_snapshot(cfg.macro_dir, list(cfg.macro_series),
                                         date.fromisoformat(as_of))}

    @server.tool()
    def get_fundamentals(symbol: str, as_of: str) -> dict:
        """Latest annual income, cash flow and balance-sheet figures from SEC filings filed
        on or before as_of, plus revenue growth, net margin and leverage."""
        try:
            facts = load_facts(cfg.fundamentals_dir, symbol.upper())
        except DataError as exc:
            return {"symbol": symbol.upper(), "as_of": as_of, "available": False,
                    "reason": str(exc)}
        out = fundamentals(facts, date.fromisoformat(as_of))
        return {"symbol": symbol.upper(), "as_of": as_of, "source": "SEC EDGAR", **out}

    return server
