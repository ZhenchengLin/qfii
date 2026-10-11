import asyncio
import json

import httpx
import pytest

from qfii.web.app import build_app


@pytest.fixture
def app(cfg):
    return build_app(cfg)


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ui.test")


async def test_page_and_status(app):
    async with client(app) as c:
        page = await c.get("/")
        assert page.status_code == 200 and "Qfii" in page.text
        status = (await c.get("/api/status")).json()
        assert status["cash_usd"] == 100000 and status["largest_allowed_order_usd"] == 10000
        assert status["models"]["neutral"] == "fake-strongest"
        assert (await c.get("/api/debates")).json() == []


async def test_live_run_streams_events_then_appears_in_history(app):
    async with client(app) as c:
        start = await c.post("/api/runs", json={"symbol": "nvda", "as_of": "2026-10-09"})
        run = start.json()
        assert run["symbol"] == "NVDA"
        busy = await c.post("/api/runs", json={"symbol": "AAPL"})
        assert busy.status_code == 409
        server = app.state.server
        for _ in range(200):
            if server.runs[run["run_id"]].done:
                break
            await asyncio.sleep(0.05)
        events = []
        async with c.stream("GET", f"/api/runs/{run['run_id']}/events") as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: ") and line != "data: {}":
                    events.append(json.loads(line[6:]))
        kinds = [e["type"] for e in events]
        assert kinds[0] == "started" and kinds[1] == "research" and kinds[-1] == "outcome"
        assert kinds.count("ballot") == 9 and kinds.count("round") == 3
        assert events[-1]["outcome"] == "invest"
        history = (await c.get("/api/debates")).json()
        assert history[0]["symbol"] == "NVDA" and history[0]["size_usd"] == 6000
        detail = (await c.get(f"/api/debates/{history[0]['id']}")).json()
        assert [r["result"] for r in detail["rounds"]] == ["request_info", "continue", "invest"]
        assert len(detail["research"]["observations"]) > 5
        assert detail["rounds"][0]["reports"][0]["team"] == "risk"


async def test_bad_ticker_rejected(app):
    async with client(app) as c:
        assert (await c.post("/api/runs", json={"symbol": "!!"})).status_code == 400
        assert (await c.get("/api/debates/999")).status_code == 404
