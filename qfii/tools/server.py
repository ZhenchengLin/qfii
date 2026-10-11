"""Qfii MCP tool server. Agents reach market data and calculations only through
these tools, so every number an agent cites came from deterministic code."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as McpToolError

from ..clock import AsOf, CutoffError
from ..config import Config
from .fundamentals import fundamentals, load_facts
from .indicators import benchmark_stats, compute_all
from .macro import macro_snapshot
from .market import DataError, load_prices
from .quality import quality_flags


def _sec(cfg: Config, cache: dict):
    """One SecSource per tool server; None (with the reason) if not configured."""
    from .sec import SecSource, SecUnavailable

    if "source" not in cache:
        try:
            cache["source"] = SecSource(cfg.sec_identity, cfg.sec_cache_dir,
                                        news_lookback_days=cfg.news_lookback_days)
        except SecUnavailable as exc:
            cache["source"], cache["reason"] = None, str(exc)
    return cache["source"], cache.get("reason")


def _cut(as_of: str) -> AsOf:
    try:
        return AsOf.parse(as_of)
    except CutoffError as exc:
        raise McpToolError(str(exc)) from exc


def build_tool_server(cfg: Config) -> MCPServer:
    sec_cache: dict = {}
    server = MCPServer(
        "qfii-tools",
        instructions="Market data, technical indicators, benchmark comparison, macro "
                     "conditions, company fundamentals and data-quality checks for Qfii. All "
                     "tools take an as_of cutoff (YYYY-MM-DD, or YYYY-MM-DDTHH:MM US Eastern) "
                     "and never return data that was not public by then.",
    )

    def _bars(symbol: str, as_of: str):
        # Errors become MCP ToolErrors: other exceptions reach the agent as a bare
        # "Error executing tool" with the reason hidden.
        cut = _cut(as_of)
        sym = symbol.upper()
        try:
            if cfg.data_source == "csv":
                from ..data_fetch import ensure_prices

                ensure_prices(cfg.csv_dir, sym, cut.day)
            return load_prices(cfg.data_source, cfg.csv_dir, sym, cut, cfg.intraday_dir)
        except DataError as exc:
            raise McpToolError(str(exc)) from exc

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
        flags = quality_flags(bars, _cut(as_of).day, source=cfg.data_source,
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
                                         _cut(as_of).day)}

    @server.tool()
    def get_fundamentals(symbol: str, as_of: str) -> dict:
        """Latest annual income, cash flow and balance-sheet figures from SEC filings filed
        on or before as_of, plus revenue growth, net margin and leverage."""
        try:
            facts = load_facts(cfg.fundamentals_dir, symbol.upper())
        except DataError as exc:
            return {"symbol": symbol.upper(), "as_of": as_of, "available": False,
                    "reason": str(exc)}
        out = fundamentals(facts, _cut(as_of).day)
        return {"symbol": symbol.upper(), "as_of": as_of, "source": "SEC EDGAR", **out}

    @server.tool()
    def calculate(expression: str, label: str = "") -> dict:
        """Evaluate arithmetic exactly (+ - * / ** %, parentheses, abs/min/max/round/
        sqrt/log/exp), e.g. market cap = price x shares, P/E = market cap / net income.
        Use this instead of computing numbers yourself; cite its result."""
        from .calc import CalcError
        from .calc import calculate as calc

        try:
            value = calc(expression)
        except CalcError as exc:
            raise McpToolError(str(exc)) from exc
        return {"expression": expression, "label": label, "value": round(value, 8)}

    @server.tool()
    def get_sec_fundamentals(symbol: str, as_of: str) -> dict:
        """XBRL financials from the latest 10-K and 10-Q filed on or before as_of:
        revenue, net and operating income, cash flow, balance sheet, growth, margins."""
        src, reason = _sec(cfg, sec_cache)
        if src is None:
            return {"symbol": symbol.upper(), "available": False, "reason": reason}
        try:
            out = src.fundamentals(symbol.upper(), _cut(as_of))
        except Exception as exc:  # noqa: BLE001 - SEC/network errors become a data gap
            return {"symbol": symbol.upper(), "available": False, "reason": str(exc)[:300]}
        return {"symbol": symbol.upper(), "as_of": as_of, "source": "SEC EDGAR (XBRL)", **out}

    @server.tool()
    def get_sec_news(symbol: str, as_of: str) -> dict:
        """Recent 8-K current reports filed on or before as_of: event items (earnings,
        executive changes, restructuring...) and the press release text."""
        src, reason = _sec(cfg, sec_cache)
        if src is None:
            return {"symbol": symbol.upper(), "available": False, "reason": reason, "news": []}
        try:
            news = src.news(symbol.upper(), _cut(as_of))
        except Exception as exc:  # noqa: BLE001
            return {"symbol": symbol.upper(), "available": False, "reason": str(exc)[:300],
                    "news": []}
        for n in news:
            n["text"] = n["text"][:3000]
        return {"symbol": symbol.upper(), "as_of": as_of, "available": True, "news": news}

    @server.tool()
    def search_sec_filings(symbol: str, as_of: str, query: str, k: int = 4) -> dict:
        """Most relevant passages for `query` from the 10-K risk factors and MD&A, the
        10-Q MD&A, and recent 8-K press releases filed on or before as_of."""
        src, reason = _sec(cfg, sec_cache)
        if src is None:
            return {"symbol": symbol.upper(), "available": False, "reason": reason, "hits": []}
        try:
            hits = src.search(symbol.upper(), _cut(as_of), query, k)
        except Exception as exc:  # noqa: BLE001
            return {"symbol": symbol.upper(), "available": False, "reason": str(exc)[:300],
                    "hits": []}
        return {"symbol": symbol.upper(), "as_of": as_of, "query": query, "available": True,
                "hits": hits}

    return server
