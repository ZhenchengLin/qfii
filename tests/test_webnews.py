from datetime import date

import httpx

from qfii.clock import AsOf
from qfii.config import load_config
from qfii.data_fetch import _TRIED, ensure_macro
from qfii.tools.client import ToolBox
from qfii.tools.webnews import clean_name, web_news

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Nvidia (NVDA) beats estimates</title><description>&lt;p&gt;Strong quarter&lt;/p&gt;</description>
  <link>https://a.test/1</link><pubDate>Wed, 07 Oct 2026 14:00:00 +0000</pubDate></item>
<item><title>Shopify stock falls</title><description>Unrelated</description>
  <link>https://a.test/2</link><pubDate>Wed, 07 Oct 2026 15:00:00 +0000</pubDate></item>
<item><title>Nvidia news from the future</title><description></description>
  <link>https://a.test/3</link><pubDate>Fri, 30 Oct 2026 15:00:00 +0000</pubDate></item>
<item><title>Nvidia old news</title><description></description>
  <link>https://a.test/4</link><pubDate>Mon, 01 Jun 2026 15:00:00 +0000</pubDate></item>
</channel></rss>"""
GDELT = {"articles": [
    {"title": "Nvidia , rivals race on chips", "url": "https://b.test/1", "domain": "b.test",
     "seendate": "20261008T120000Z"},
    {"title": "Nvidia (NVDA) beats estimates", "url": "https://b.test/2", "domain": "b.test",
     "seendate": "20261007T120000Z"}]}


def client(gdelt=lambda: httpx.Response(200, json=GDELT),
           rss=lambda: httpx.Response(200, text=RSS)):
    def handler(req: httpx.Request) -> httpx.Response:
        if "chart" in req.url.path:
            return httpx.Response(200, json={"chart": {"result": [
                {"meta": {"longName": "NVIDIA Corporation"}}]}})
        return gdelt() if "gdelt" in req.url.host else rss()
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_clean_name():
    assert clean_name("NVIDIA Corporation") == "NVIDIA"
    assert clean_name("Apple Inc.") == "Apple"
    assert clean_name("Alphabet Inc. Class A") == "Alphabet"


def test_keeps_only_relevant_news_public_at_the_cutoff():
    out = web_news("nvda", AsOf.parse("2026-10-09"), http=client())
    assert out["available"] and out["company"] == "NVIDIA"
    titles = [n["title"] for n in out["news"]]
    # newest first; no unrelated story, no future or stale article, no duplicate;
    # the spacing GDELT leaves before punctuation is cleaned
    assert titles == ["Nvidia, rivals race on chips", "Nvidia (NVDA) beats estimates"]


def test_minute_cutoff_excludes_later_that_day():
    out = web_news("NVDA", AsOf.parse("2026-10-07T09:45"), http=client())   # 13:45 UTC
    # Yahoo's copy published at 14:00 is too late; GDELT saw the same story at 12:00
    assert [n["publisher"] for n in out["news"]] == ["b.test"]


def test_one_source_failing_is_not_an_error():
    out = web_news("NVDA", AsOf.parse("2026-10-09"),
                   http=client(gdelt=lambda: httpx.Response(200, text="phrase too short")))
    assert out["available"] and out["sources_failed"] and len(out["news"]) == 1


def test_gdelt_rate_limit_waits_and_retries(monkeypatch):
    monkeypatch.setattr("qfii.tools.webnews.time.sleep", lambda s: None)
    replies = [httpx.Response(429), httpx.Response(200, json=GDELT)]
    out = web_news("NVDA", AsOf.parse("2026-10-09"), http=client(gdelt=lambda: replies.pop(0)))
    assert not out["sources_failed"] and len(out["news"]) == 2


def test_all_sources_failing_reports_unavailable():
    out = web_news("NVDA", AsOf.parse("2026-10-09"),
                   http=client(gdelt=lambda: httpx.Response(500),
                               rss=lambda: httpx.Response(500)))
    assert not out["available"] and out["news"] == []


async def test_tool_is_off_unless_configured_and_makes_no_request(cfg, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("went online")
    monkeypatch.setattr("qfii.tools.webnews.web_news", boom)
    async with ToolBox(cfg) as tools:
        out = await tools.call("get_web_news", symbol="NVDA", as_of="2026-10-09")
    assert not out["available"] and "online_research" in out["reason"]


async def test_research_evidence_labels_news_as_unverified(tmp_path, monkeypatch):
    from qfii.agents.evidence import collect_evidence

    p = tmp_path / "c.toml"
    p.write_text(f'[data]\nonline_research = true\nmacro_dir = "{tmp_path}/macro"\n'
                 f'macro_series = []\n')
    monkeypatch.setattr("qfii.tools.webnews.web_news", lambda s, c, n: web_news(s, c, n, client()))
    async with ToolBox(load_config(p)) as tools:
        got = await collect_evidence(tools, "NVDA", "2026-10-09")
    rows = [f for f in got.facts if f["name"] == "web_news"]
    assert len(rows) == 2 and "unverified" in rows[0]["label"]
    assert "no_news" not in got.flags
    assert all(e.kind == "news" for e in got.evidence if e.source.startswith("mcp:get_web_news"))


def test_macro_downloads_missing_series_and_survives_failure(tmp_path):
    _TRIED.clear()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.params["id"] == "BAD":
            return httpx.Response(500)
        return httpx.Response(200, text="date,v\n2026-10-01,4.1\n2026-10-02,.\n")
    http = httpx.Client(transport=httpx.MockTransport(handler))
    ensure_macro(tmp_path, ["DGS10", "BAD"], http)
    assert (tmp_path / "DGS10.csv").exists() and not (tmp_path / "BAD.csv").exists()
    assert date(2026, 10, 1).isoformat() in (tmp_path / "DGS10.csv").read_text()
    _TRIED.clear()
