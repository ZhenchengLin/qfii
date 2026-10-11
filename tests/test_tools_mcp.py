import pytest

from qfii.tools.client import ToolBox, ToolError
from tests.conftest import AS_OF


async def test_tools_listed_and_callable_over_mcp(cfg):
    async with ToolBox(cfg) as tools:
        assert set(await tools.list()) == {
            "get_price_history", "compute_indicators", "check_data_quality",
            "compare_to_benchmark", "get_macro_snapshot", "get_fundamentals"}
        ind = await tools.call("compute_indicators", symbol="aapl", as_of=AS_OF)
        assert ind["symbol"] == "AAPL" and ind["last_date"] <= AS_OF
        assert set(ind["indicators"]) >= {"rsi_14", "sma_200", "volatility_3m_ann"}
        q = await tools.call("check_data_quality", symbol="AAPL", as_of=AS_OF)
        assert q["flags"] == ["synthetic_data"]
        h = await tools.call("get_price_history", symbol="AAPL", as_of=AS_OF, last_n=5)
        assert len(h["bars"]) == 5 and h["n"] == 400
        assert [c["tool"] for c in tools.calls] == ["compute_indicators", "check_data_quality",
                                                    "get_price_history"]


async def test_tool_error_surfaces(cfg):
    async with ToolBox(cfg) as tools:
        with pytest.raises(ToolError):
            await tools.call("compute_indicators", symbol="AAPL", as_of="not-a-date")
