"""Online news for the research teams, from public sources that need no key.

  Yahoo Finance RSS   the latest headlines for a ticker (recent cutoffs only)
  GDELT DOC API       an open index of world news, searchable by date range

Only what was public at the cutoff is returned: an article counts from its
published date (Yahoo) or the moment GDELT first saw it. Headlines are third-party
reports and may be wrong, so they are labelled that way wherever they are shown.
One source failing is not an error; both failing makes the tool report unavailable.
"""

from __future__ import annotations

import html
import re
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..clock import AsOf

YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"
USER_AGENT = "Mozilla/5.0 (compatible; qfii-research/0.1)"
WINDOW_DAYS = 30
GDELT_WAIT_S = 6       # GDELT allows one request per 5 seconds
GDELT_TRIES = 3
_SUFFIX = re.compile(r"[,.]?\s+(corporation|corp|incorporated|inc|company|co|holdings|group|"
                     r"limited|ltd|plc|n\.?v|s\.?a|class [a-c]|common stock)\.?$", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?])")


def clean_name(name: str) -> str:
    """'NVIDIA Corporation' -> 'NVIDIA'."""
    name = name.strip()
    while (short := _SUFFIX.sub("", name)) != name:
        name = short
    return name


def _text(value: str | None, cap: int) -> str:
    plain = html.unescape(_TAGS.sub(" ", value or ""))
    return _SPACE_BEFORE_PUNCT.sub(r"\1", " ".join(plain.split()))[:cap]


def parse_yahoo_rss(xml_text: str) -> list[dict[str, Any]]:
    items = []
    for it in ET.fromstring(xml_text).iter("item"):
        try:
            published = parsedate_to_datetime(it.findtext("pubDate") or "")
        except (TypeError, ValueError):
            continue
        items.append({"title": _text(it.findtext("title"), 300),
                      "summary": _text(it.findtext("description"), 500),
                      "url": it.findtext("link") or "", "publisher": "Yahoo Finance feed",
                      "published": published})
    return items


def parse_gdelt(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for a in payload.get("articles", []):
        try:
            seen = datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except (KeyError, ValueError):
            continue
        items.append({"title": _text(a.get("title"), 300), "summary": "",
                      "url": a.get("url", ""), "publisher": a.get("domain", "unknown"),
                      "published": seen})
    return items


def relevant(item: dict[str, Any], symbol: str, name: str) -> bool:
    """Ticker feeds also carry unrelated stories: keep what names the company."""
    text = f"{item['title']} {item['summary']}"
    if name and re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE):
        return True
    return len(symbol) >= 3 and re.search(rf"\b{re.escape(symbol)}\b", text) is not None


def company_name(http: httpx.Client, symbol: str) -> str:
    try:
        resp = http.get(YAHOO_CHART.format(symbol=symbol), params={"range": "1d"})
        meta = resp.json()["chart"]["result"][0]["meta"]
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        return ""
    return clean_name(meta.get("longName") or meta.get("shortName") or "")


def _yahoo(http: httpx.Client, symbol: str) -> list[dict[str, Any]]:
    resp = http.get(YAHOO_RSS, params={"s": symbol, "region": "US", "lang": "en-US"})
    resp.raise_for_status()
    return parse_yahoo_rss(resp.text)


def _gdelt(http: httpx.Client, name: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    fmt = "%Y%m%d%H%M%S"
    params = {"query": f'"{name}" stock sourcelang:english', "mode": "ArtList",
              "format": "json", "maxrecords": 50, "sort": "DateDesc",
              "startdatetime": start.astimezone(UTC).strftime(fmt),
              "enddatetime": end.astimezone(UTC).strftime(fmt)}
    for attempt in range(GDELT_TRIES):
        resp = http.get(GDELT_DOC, params=params)
        if resp.status_code != 429 or attempt == GDELT_TRIES - 1:
            break
        time.sleep(GDELT_WAIT_S)
    resp.raise_for_status()
    return parse_gdelt(resp.json())   # errors come back as plain text: ValueError


def web_news(symbol: str, cut: AsOf, limit: int = 12,
             http: httpx.Client | None = None) -> dict[str, Any]:
    client = http or httpx.Client(timeout=20, follow_redirects=True,
                                  headers={"User-Agent": USER_AGENT})
    symbol = symbol.upper()
    name = company_name(client, symbol)
    start = cut.end - timedelta(days=WINDOW_DAYS)
    found: list[dict[str, Any]] = []
    failed: list[str] = []
    sources = [("Yahoo Finance", lambda: _yahoo(client, symbol))]
    if name:   # GDELT rejects short terms such as most tickers
        sources.append(("GDELT", lambda: _gdelt(client, name, start, cut.end)))
    for label, fetch in sources:
        try:
            found += fetch()
        except (httpx.HTTPError, ValueError, ET.ParseError) as exc:
            failed.append(f"{label}: {str(exc)[:120]}")
    if failed and len(failed) == len(sources):
        return {"symbol": symbol, "available": False, "reason": "; ".join(failed), "news": []}

    seen: set[str] = set()
    news = []
    for item in sorted(found, key=lambda i: i["published"], reverse=True):
        key = re.sub(r"\W+", "", item["title"].lower())
        if not key or key in seen or not cut.covers(item["published"]) or \
                item["published"] < start or not relevant(item, symbol, name):
            continue
        seen.add(key)
        news.append({**item, "published": item["published"].astimezone(UTC)
                     .strftime("%Y-%m-%dT%H:%MZ")})
        if len(news) == limit:
            break
    return {"symbol": symbol, "company": name, "as_of": cut.iso(), "available": True,
            "news": news, "sources_failed": failed}
