"""Build the offline dataset for the week replay, in exactly the formats the MCP
tools read, so the replay needs no network and can be copied (or uploaded) as one
folder.

  <dataset_dir>/
    universe.json            the draw: seed, eligible companies, the 10 picked,
                             and why others were excluded
    prices/<SYM>.csv         daily bars, date,close,volume (tools: data.csv_dir)
    intraday/<SYM>.csv       10-minute bars, time,open,high,low,close,volume;
                             time = when the bar closed, US Eastern
    macro/<SERIES>.csv       FRED series (tools: data.macro_dir)
    sec/<SYM>/filings.json   filings the week can see, with SEC acceptance times
    sec/<SYM>/<acc>.*.json   parsed filings: metrics, passages, 8-K text
    manifest.json            what was built, sizes per market day, sources

Prices come from raw Yahoo downloads saved under week.raw_dir (1-minute bars are
only kept by Yahoo for a few weeks, so they are saved first, then processed).
Daily history before the week is the dividend/split-adjusted close rescaled to
the raw price level of the last day before the week, so it joins the raw
intraday prices without a jump; days inside the week use the raw close.
"""

from __future__ import annotations

import csv
import json
import random
import shutil
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..clock import ET, MARKET_CLOSE, MARKET_OPEN, AsOf
from ..config import Config

UA = {"User-Agent": "Mozilla/5.0"}
NASDAQ100_URL = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
MIN_HISTORY_DAYS = 252          # a year of daily bars for the 200-day indicators
MIN_BARS_PER_DAY = 30           # of 39 ten-minute bars: the stock traded normally


class DatasetError(RuntimeError):
    pass


# ------------------------------------------------------------------ raw downloads

def _get_json(url: str, headers: dict[str, str] | None = None) -> Any:
    req = urllib.request.Request(url, headers=headers or UA)
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def download_raw(cfg: Config, symbols: list[str] | None = None, log=print) -> Path:
    """Save the Nasdaq-100 list and raw Yahoo 1-minute (week) and daily (5 years)
    bars. Existing files are kept, so this can be re-run safely."""
    w = cfg.week
    raw = w.raw_dir
    (raw / "yahoo_1m").mkdir(parents=True, exist_ok=True)
    (raw / "yahoo_1d").mkdir(parents=True, exist_ok=True)
    listing = raw / "nasdaq100.json"
    if not listing.exists():
        data = _get_json(NASDAQ100_URL, {**UA, "Accept": "application/json"})["data"]
        listing.write_text(json.dumps(data, indent=1))
    if symbols is None:
        symbols = [r["symbol"] for r in json.loads(listing.read_text())["data"]["rows"]]
    symbols = list(dict.fromkeys(symbols + [cfg.portfolio.benchmark]))
    p1 = int(datetime.combine(w.start, MARKET_OPEN, ET).timestamp()) - 3600
    p2 = int(datetime.combine(w.end, MARKET_CLOSE, ET).timestamp()) + 3600
    failed = []
    for sym in symbols:
        for kind, query in (("yahoo_1m", f"interval=1m&period1={p1}&period2={p2}"),
                            ("yahoo_1d", "interval=1d&range=5y&events=div,split")):
            path = raw / kind / f"{sym}.json"
            if path.exists():
                continue
            for attempt in range(3):
                try:
                    path.write_text(json.dumps(_get_json(f"{CHART_URL.format(symbol=sym)}?"
                                                         f"{query}")))
                    break
                except Exception:  # noqa: BLE001 - retried, then reported
                    time.sleep(2 + 3 * attempt)
            else:
                failed.append(f"{sym} {kind}")
            time.sleep(0.25)
    if failed:
        log(f"raw download failed for: {', '.join(failed)}")
    return raw


# ------------------------------------------------------------------ price processing

def _chart(payload: dict, symbol: str) -> dict:
    chart = payload.get("chart", {})
    if chart.get("error") or not chart.get("result"):
        raise DatasetError(f"{symbol}: no chart data ({chart.get('error')})")
    return chart["result"][0]


def daily_rows(payload: dict, symbol: str, week_start: date) -> list[dict[str, Any]]:
    """date, close, volume. Before the week: adjusted close rescaled to the raw level
    of the last pre-week day; inside the week and after: raw close."""
    r = _chart(payload, symbol)
    quote = r["indicators"]["quote"][0]
    adj = (r["indicators"].get("adjclose") or [{}])[0].get("adjclose") or quote["close"]
    rows = []
    for ts, raw_close, adj_close, vol in zip(r.get("timestamp") or [], quote["close"], adj,
                                             quote["volume"], strict=False):
        if raw_close is None or adj_close is None:
            continue
        day = datetime.fromtimestamp(ts, UTC).astimezone(ET).date()
        rows.append((day, float(raw_close), float(adj_close), float(vol or 0)))
    before = [x for x in rows if x[0] < week_start]
    if not before:
        raise DatasetError(f"{symbol}: no daily history before {week_start}")
    factor = before[-1][1] / before[-1][2]
    return [{"date": d.isoformat(),
             "close": round(adj * factor if d < week_start else raw_close, 4),
             "volume": vol} for d, raw_close, adj, vol in rows]


def intraday_rows(payload: dict, symbol: str, minutes: int = 10) -> list[dict[str, Any]]:
    """Aggregate 1-minute bars into `minutes` bars stamped with their closing time
    (09:40 covers 09:30-09:39). The 16:00 closing print joins the last bar."""
    r = _chart(payload, symbol)
    q = r["indicators"]["quote"][0]
    buckets: dict[datetime, list[tuple]] = {}
    for i, ts in enumerate(r.get("timestamp") or []):
        if q["close"][i] is None:
            continue
        t = datetime.fromtimestamp(ts, UTC).astimezone(ET)
        open_ = datetime.combine(t.date(), MARKET_OPEN, ET)
        close = datetime.combine(t.date(), MARKET_CLOSE, ET)
        if t < open_ or t > close:
            continue
        n = int((t - open_).total_seconds() // (minutes * 60)) + 1
        end = min(open_ + timedelta(minutes=minutes * n), close)
        buckets.setdefault(end, []).append(
            (q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i] or 0))
    out = []
    for end in sorted(buckets):
        b = buckets[end]
        highs = [x[1] for x in b if x[1] is not None]
        lows = [x[2] for x in b if x[2] is not None]
        out.append({"time": end.isoformat(), "open": round(b[0][0] or b[0][3], 4),
                    "high": round(max(highs or [x[3] for x in b]), 4),
                    "low": round(min(lows or [x[3] for x in b]), 4),
                    "close": round(b[-1][3], 4), "volume": float(sum(x[4] for x in b))})
    return out


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


# ------------------------------------------------------------------ the draw

@dataclass
class Draw:
    seed: int
    eligible: list[str]
    picked: list[str]
    excluded: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)


def _company(name: str) -> str:
    """'Alphabet Inc. Class A Common Stock' -> 'alphabet inc.' (one per company)."""
    low = name.lower()
    for cut in (" class ", " common stock", " capital stock", " ordinary shares",
                " american depositary", " ads"):
        if cut in low:
            low = low[:low.index(cut)]
    return low.strip(" ,")


def price_checks(raw: Path, symbol: str, start: date, end: date, minutes: int) -> str | None:
    """None if the stock has enough history and traded normally all week."""
    try:
        daily = daily_rows(json.loads((raw / "yahoo_1d" / f"{symbol}.json").read_text()),
                           symbol, start)
        bars = intraday_rows(json.loads((raw / "yahoo_1m" / f"{symbol}.json").read_text()),
                             symbol, minutes)
    except (OSError, DatasetError, KeyError, ValueError) as exc:
        return f"no usable price data ({exc})"[:160]
    history = sum(1 for r in daily if r["date"] < start.isoformat())
    if history < MIN_HISTORY_DAYS:
        return f"only {history} trading days of history before {start} (needs " \
               f"{MIN_HISTORY_DAYS})"
    per_day: dict[str, int] = {}
    for b in bars:
        per_day[b["time"][:10]] = per_day.get(b["time"][:10], 0) + 1
    d = start
    while d <= end:
        if d.weekday() < 5 and per_day.get(d.isoformat(), 0) < MIN_BARS_PER_DAY:
            return f"incomplete intraday data on {d} ({per_day.get(d.isoformat(), 0)} bars)"
        d += timedelta(days=1)
    return None


def files_10k_10q(company: Any, start: date) -> str | None:
    """None if the company files US annual and quarterly reports (10-K / 10-Q)."""
    def latest(form: str) -> date | None:
        for f in company.get_filings(form=form) or []:
            if str(f.form).upper() != form:
                continue
            filed = f.filing_date if isinstance(f.filing_date, date) else \
                date.fromisoformat(str(f.filing_date)[:10])
            if filed <= start:
                return filed
        return None

    k = latest("10-K")
    if k is None or (start - k).days > 400:
        return "no 10-K in the last 13 months (foreign filer: 20-F / 40-F?)"
    q = latest("10-Q")
    if (q is None or (start - q).days > 200) and (start - k).days > 200:
        return "no recent 10-Q"
    return None


def draw(cfg: Config, company_factory: Callable[[str], Any], log=print) -> Draw:
    """Random `week.symbols` companies (fixed seed) among those our tools can serve:
    a year of prices, a normal trading week, and US 10-K / 10-Q filings."""
    w = cfg.week
    rows = json.loads((w.raw_dir / "nasdaq100.json").read_text())["data"]["rows"]
    names = {r["symbol"]: r.get("companyName", r["symbol"]) for r in rows}
    excluded: dict[str, str] = {}
    by_company: dict[str, list[str]] = {}
    for sym, name in names.items():
        by_company.setdefault(_company(name), []).append(sym)
    candidates = []
    for syms in by_company.values():
        keep = max(syms, key=len)       # GOOGL over GOOG
        for other in syms:
            if other != keep:
                excluded[other] = f"second share class of {keep}"
        candidates.append(keep)
    eligible = []
    for sym in sorted(candidates):
        reason = price_checks(w.raw_dir, sym, w.start, w.end, w.cadence_minutes)
        if reason:
            excluded[sym] = reason
        else:
            eligible.append(sym)
    order = list(eligible)
    random.Random(w.seed).shuffle(order)
    picked: list[str] = []
    for sym in order:
        if len(picked) == w.symbols:
            break
        try:
            reason = files_10k_10q(company_factory(sym), w.start)
        except Exception as exc:  # noqa: BLE001 - not an SEC filer
            reason = f"not found at the SEC ({exc})"[:160]
        if reason:
            excluded[sym] = reason
            log(f"  skip {sym}: {reason}")
        else:
            picked.append(sym)
            log(f"  pick {sym}: {names[sym]}")
    if len(picked) < w.symbols:
        raise DatasetError(f"only {len(picked)} eligible companies, wanted {w.symbols}")
    return Draw(w.seed, eligible, picked, excluded, {s: names[s] for s in picked})


# ------------------------------------------------------------------ SEC filings

def _filed(f: Any) -> date:
    return f.filing_date if isinstance(f.filing_date, date) else \
        date.fromisoformat(str(f.filing_date)[:10])


def select_filings(company: Any, start: date, end: date, lookback_days: int) -> list[Any]:
    """Every filing the week can see: the latest 10-K and 10-Q as of the start plus
    any filed during the week, and 8-Ks within the news lookback."""
    chosen = []
    for form in ("10-K", "10-Q"):
        for f in company.get_filings(form=form) or []:
            if str(f.form).upper() != form or _filed(f) > end:
                continue
            chosen.append(f)
            if _filed(f) < start:
                break
    since = start - timedelta(days=lookback_days)
    for f in company.get_filings(form="8-K") or []:
        if str(f.form).upper() != "8-K" or _filed(f) > end:
            continue
        if _filed(f) < since:
            break
        chosen.append(f)
    return chosen


def build_sec(cfg: Config, symbol: str, sec_dir: Path, company_factory=None,
              log=print) -> dict[str, Any]:
    """Parse and cache every filing the week can see; write filings.json with SEC
    acceptance times. After this the tools read the symbol offline."""
    from ..tools.sec import SecSource

    src = SecSource(cfg.sec_identity, sec_dir, company_factory,
                    news_lookback_days=cfg.news_lookback_days)
    company = src.company(symbol)
    w = cfg.week
    rows = []
    for f in select_filings(company, w.start, w.end, cfg.news_lookback_days):
        accepted = src.accepted(symbol, f)               # index page, cached
        src._meta(symbol, f)
        if f.form in ("10-K", "10-Q"):
            src._cached(symbol, f, "metrics.v2", lambda f=f: src._statement_metrics(f))
            src._cached(symbol, f, "passages", lambda f=f: src._sections(f))
        else:
            src._cached(symbol, f, "8k", lambda f=f: src._eight_k(f))
        rows.append({"form": f.form, "accession": f.accession_no,
                     "filing_date": _filed(f).isoformat(),
                     "accepted": accepted.replace(tzinfo=None).isoformat() if accepted else None})
        time.sleep(0.12)                                  # SEC fair-access: < 10 req/s
    (sec_dir / symbol).mkdir(parents=True, exist_ok=True)
    (sec_dir / symbol / "filings.json").write_text(json.dumps(
        {"symbol": symbol, "entity": getattr(company, "name", symbol),
         "built_at": datetime.now(UTC).isoformat(timespec="seconds"), "filings": rows},
        indent=1))
    in_week = [r for r in rows if r["filing_date"] >= w.start.isoformat()]
    log(f"  {symbol}: {len(rows)} filings cached, {len(in_week)} filed during the week")
    return {"filings": len(rows), "in_week": in_week}


# ------------------------------------------------------------------ sizes

def dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def storage_report(out: Path, days: int, gb_per_day: float) -> dict[str, Any]:
    parts = {name: dir_bytes(out / name) for name in ("prices", "intraday", "macro", "sec")}
    total = sum(parts.values()) + sum(p.stat().st_size for p in out.glob("*.json"))
    per_day = total / max(days, 1)
    return {"bytes": parts, "total_bytes": total, "market_days": days,
            "bytes_per_market_day": round(per_day), "gb_per_day_limit": gb_per_day,
            "within_limit": per_day <= gb_per_day * 1024 ** 3}


# ------------------------------------------------------------------ the build

def build(cfg: Config, *, download: bool = True, company_factory=None, log=print
          ) -> dict[str, Any]:
    w = cfg.week
    out = w.dataset_dir
    if download:
        log("1/5 raw downloads (Nasdaq-100 list, Yahoo 1-minute and daily bars)")
        download_raw(cfg, log=log)
    if company_factory is None:
        from ..tools.sec import SecSource

        company_factory = SecSource(cfg.sec_identity, out / "sec").company
    log(f"2/5 random draw of {w.symbols} (seed {w.seed})")
    picked = draw(cfg, company_factory, log)
    symbols = picked.picked
    log("3/5 prices: daily and 10-minute bars")
    days = []
    d = w.start
    while d <= w.end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    for sym in symbols + [cfg.portfolio.benchmark]:
        daily = daily_rows(json.loads((w.raw_dir / "yahoo_1d" / f"{sym}.json").read_text()),
                           sym, w.start)
        _write_csv(out / "prices" / f"{sym}.csv",
                   [r for r in daily if r["date"] <= w.end.isoformat()],
                   ["date", "close", "volume"])
        bars = intraday_rows(json.loads((w.raw_dir / "yahoo_1m" / f"{sym}.json").read_text()),
                             sym, w.cadence_minutes)
        _write_csv(out / "intraday" / f"{sym}.csv",
                   [b for b in bars if w.start.isoformat() <= b["time"][:10]
                    <= w.end.isoformat()],
                   ["time", "open", "high", "low", "close", "volume"])
    log("4/5 macro series (FRED)")
    macro_out = out / "macro"
    macro_out.mkdir(parents=True, exist_ok=True)
    missing = [s for s in cfg.macro_series if not (macro_out / f"{s}.csv").exists()]
    if missing and download:
        import httpx

        from ..data_fetch import fetch_fred_series, write_series

        with httpx.Client(timeout=30, follow_redirects=True) as http:
            for s in missing:
                write_series(macro_out, s, fetch_fred_series(http, s))
    elif missing:
        for s in missing:                     # offline: reuse the default macro folder
            src = Path("data/macro") / f"{s}.csv"
            if src.exists():
                shutil.copy2(src, macro_out / f"{s}.csv")
    log("5/5 SEC filings (parsed once, with acceptance times)")
    sec = {sym: build_sec(cfg, sym, out / "sec", company_factory, log) for sym in symbols}
    manifest = {
        "window": {"start": w.start.isoformat(), "end": w.end.isoformat(),
                   "market_days": [x.isoformat() for x in days],
                   "cadence_minutes": w.cadence_minutes},
        "symbols": symbols, "names": picked.names, "benchmark": cfg.portfolio.benchmark,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sources": {"prices": "Yahoo Finance chart API (1m aggregated to 10m; daily 5y)",
                    "filings": "SEC EDGAR via EdgarTools; acceptance times from filing "
                               "index pages (US Eastern)",
                    "macro": "FRED", "constituents": "Nasdaq.com Nasdaq-100 list"},
        "filings_during_week": {s: v["in_week"] for s, v in sec.items()},
    }
    (out / "universe.json").write_text(json.dumps(
        {"seed": picked.seed, "picked": symbols, "names": picked.names,
         "eligible": picked.eligible, "excluded": picked.excluded}, indent=1))
    manifest["storage"] = storage_report(out, len(days), w.gb_per_day)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    if not manifest["storage"]["within_limit"]:
        raise DatasetError(f"dataset is {manifest['storage']['bytes_per_market_day']:,} bytes "
                           f"per market day, over the {w.gb_per_day} GB limit")
    return manifest


def check(cfg: Config, slots: list[AsOf] | None = None) -> list[str]:
    """Problems found reading the built dataset the way the tools will."""
    from ..tools.market import intraday_prices, load_prices

    w = cfg.week
    out = w.dataset_dir
    manifest = json.loads((out / "manifest.json").read_text())
    problems = []
    for sym in manifest["symbols"] + [manifest["benchmark"]]:
        bars = intraday_prices(out / "intraday", sym)
        for day in manifest["window"]["market_days"]:
            n = sum(1 for b in bars if b.time.date().isoformat() == day)
            if n < MIN_BARS_PER_DAY:
                problems.append(f"{sym} {day}: {n} ten-minute bars")
        for cut in slots or []:
            got = load_prices("csv", out / "prices", sym, cut, out / "intraday")
            if not got or got[-1].date > cut.day.isoformat():
                problems.append(f"{sym} at {cut}: bad cutoff")
        if sym in manifest["symbols"] and not (out / "sec" / sym / "filings.json").exists():
            problems.append(f"{sym}: no SEC manifest")
    return problems
