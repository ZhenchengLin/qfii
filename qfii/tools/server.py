"""Qfii MCP tool server. Agents reach market data and calculations only through
these tools, so every number an agent cites came from deterministic code."""

from __future__ import annotations

from datetime import date

from mcp.server.mcpserver import MCPServer

from ..config import Config
from .indicators import compute_all
from .market import load_prices
from .quality import quality_flags


def build_tool_server(cfg: Config) -> MCPServer:
    server = MCPServer(
        "qfii-tools",
        instructions="Market data, technical indicators and data-quality checks for Qfii. "
                     "All tools take an as_of date (YYYY-MM-DD) and never return later data.",
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

    return server
