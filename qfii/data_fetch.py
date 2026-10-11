"""Download real market data into local files. Agents never call the internet:
`qfii data fetch` fills data/ once, and the MCP tools read those files with the
same as_of cutoff as everything else, so runs are reproducible.

  prices        Yahoo Finance chart API (unofficial, no key) -> data/prices/<SYM>.csv
  macro         FRED fredgraph CSV (no key)                  -> data/macro/<ID>.csv
  fundamentals  SEC EDGAR companyfacts (needs a contact      -> data/fundamentals/<SYM>.json
                User-Agent, per SEC fair-access policy)
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from .tools.market import Bar

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
BROWSER_UA = "Mozilla/5.0 (compatible; qfii-research/0.1)"


class FetchError(RuntimeError):
    pass


def _get(http: httpx.Client, url: str, **kwargs) -> httpx.Response:
    try:
        resp = http.get(url, **kwargs)
    except httpx.HTTPError as exc:
        raise FetchError(f"{url}: {exc}") from exc
    if resp.status_code != 200:
        raise FetchError(f"{url}: HTTP {resp.status_code}: {resp.text[:200]}")
    return resp


# ---------------------------------------------------------------- prices

def fetch_yahoo_prices(http: httpx.Client, symbol: str, range_: str = "5y") -> list[Bar]:
    """Daily bars; close is the split- and dividend-adjusted close."""
    try:
        resp = _get(http, YAHOO_URL.format(symbol=symbol),
                    params={"range": range_, "interval": "1d", "events": "div,splits"},
                    headers={"User-Agent": BROWSER_UA})
    except FetchError as exc:
        if "HTTP 404" in str(exc):
            raise FetchError(f"{symbol} not found on Yahoo Finance (wrong or delisted "
                             f"ticker?)") from exc
        raise
    chart = resp.json().get("chart", {})
    if chart.get("error") or not chart.get("result"):
        raise FetchError(f"{symbol}: {chart.get('error') or 'no data'}")
    result = chart["result"][0]
    offset = timedelta(seconds=result["meta"].get("gmtoffset", 0))
    quote = result["indicators"]["quote"][0]
    adj = (result["indicators"].get("adjclose") or [{}])[0].get("adjclose") or quote["close"]
    bars = []
    for ts, close, volume in zip(result.get("timestamp") or [], adj, quote["volume"],
                                 strict=False):
        if close is None:  # Yahoo leaves holes for halted days
            continue
        day = (datetime.fromtimestamp(ts, UTC) + offset).date().isoformat()
        bars.append(Bar(day, round(float(close), 4), float(volume or 0)))
    if not bars:
        raise FetchError(f"{symbol}: empty price history")
    return bars


def write_prices(csv_dir: Path, symbol: str, bars: list[Bar]) -> Path:
    csv_dir.mkdir(parents=True, exist_ok=True)
    path = csv_dir / f"{symbol}.csv"
    by_date = {}
    if path.exists():
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                by_date[row["date"]] = row
    for b in bars:  # newer download wins (adjusted history can be restated)
        by_date[b.date] = {"date": b.date, "close": b.close, "volume": b.volume}
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["date", "close", "volume"])
        w.writeheader()
        for day in sorted(by_date):
            w.writerow(by_date[day])
    return path


# ---------------------------------------------------------------- macro

def fetch_fred_series(http: httpx.Client, series_id: str) -> list[tuple[str, float]]:
    resp = _get(http, FRED_URL, params={"id": series_id})
    rows = list(csv.reader(resp.text.splitlines()))
    if not rows or len(rows[0]) < 2:
        raise FetchError(f"FRED {series_id}: unexpected response")
    out = []
    for day, value in rows[1:]:
        if value in ("", "."):  # FRED marks missing observations with "."
            continue
        out.append((day, float(value)))
    if not out:
        raise FetchError(f"FRED {series_id}: no observations")
    return out


def write_series(macro_dir: Path, series_id: str, rows: list[tuple[str, float]]) -> Path:
    macro_dir.mkdir(parents=True, exist_ok=True)
    path = macro_dir / f"{series_id}.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "value"])
        w.writerows(rows)
    return path


# ---------------------------------------------------------------- fundamentals

def fetch_sec_ciks(http: httpx.Client, user_agent: str) -> dict[str, int]:
    data = _get(http, SEC_TICKERS_URL, headers={"User-Agent": user_agent}).json()
    return {row["ticker"].upper(): int(row["cik_str"]) for row in data.values()}


def fetch_company_facts(http: httpx.Client, cik: int, user_agent: str) -> dict:
    return _get(http, SEC_FACTS_URL.format(cik=cik), headers={"User-Agent": user_agent}).json()


def write_facts(fund_dir: Path, symbol: str, facts: dict) -> Path:
    fund_dir.mkdir(parents=True, exist_ok=True)
    path = fund_dir / f"{symbol}.json"
    path.write_text(json.dumps(facts))
    return path


_TRIED: set[tuple[str, str]] = set()   # (symbol, target date) already fetched this process
STALE_AFTER_DAYS = 4


def _last_date(path: Path) -> date | None:
    with path.open() as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    return date.fromisoformat(lines[-1].split(",")[0]) if len(lines) > 1 else None


def ensure_prices(csv_dir: Path, symbol: str, as_of: date,
                  http: httpx.Client | None = None) -> None:
    """Download prices for a symbol on first use, or when they are stale for as_of.
    Raises DataError if there is still no price file afterwards."""
    from .tools.market import DataError

    path = csv_dir / f"{symbol}.csv"
    target = min(as_of, date.today())
    if path.exists():
        last = _last_date(path)
        if last and (target - last).days <= STALE_AFTER_DAYS:
            return
    key = (symbol, target.isoformat())
    if key in _TRIED:
        if path.exists():
            return
        raise DataError(f"no price data for {symbol} (download failed earlier)")
    _TRIED.add(key)
    client = http or httpx.Client(timeout=30, follow_redirects=True)
    try:
        write_prices(csv_dir, symbol, fetch_yahoo_prices(client, symbol))
    except FetchError as exc:
        if not path.exists():
            raise DataError(f"no price data for {symbol}: {exc}") from exc
    finally:
        if http is None:
            client.close()
