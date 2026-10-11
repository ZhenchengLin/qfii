"""SEC filings as evidence: XBRL financials, filing text, and 8-K "news".

  fundamentals  latest 10-K (annual) and 10-Q (quarterly) XBRL statements, via
                EdgarTools, with year-over-year growth and margins
  news          recent 8-K current reports: event items (earnings, executive
                changes, restructuring...) and the EX-99.1 press release, converted
                to text with Docling when installed
  search        keyword (BM25) search over passages from the 10-K risk factors and
                MD&A, the 10-Q MD&A, and recent 8-K press releases

Point in time: only filings with filing_date <= as_of are used. Parsed results are
cached per filing (accession number) under data.sec_cache_dir.
Needs `pip install qfii[sec]` (EdgarTools) and data.sec_user_agent, a contact
identity the SEC requires on every request. Docling (`qfii[docling]`) is optional.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ITEM_LABELS = {
    "1.01": "entry into a material agreement", "1.02": "termination of a material agreement",
    "1.05": "material cybersecurity incident", "2.01": "acquisition or disposition of assets",
    "2.02": "results of operations (earnings)", "2.03": "new financial obligation (debt)",
    "2.05": "exit or restructuring costs", "2.06": "material impairment",
    "3.01": "delisting notice", "4.01": "auditor change",
    "4.02": "prior financials no longer reliable (restatement)",
    "5.02": "director or officer change", "5.07": "shareholder vote results",
    "7.01": "Regulation FD disclosure", "8.01": "other events",
    "9.01": "financial statements and exhibits",
}
PASSAGE_CHARS = 900
log = logging.getLogger(__name__)
for _noisy in ("edgar", "httpx", "httpcore"):  # EdgarTools logs every request at INFO
    logging.getLogger(_noisy).setLevel(logging.WARNING)


class SecUnavailable(RuntimeError):
    pass


# ---------------------------------------------------------------- text utilities

def chunk(text: str, size: int = PASSAGE_CHARS) -> list[str]:
    """Split on paragraphs, then pack paragraphs into passages of about `size` chars."""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?=[A-Z][^\n]{0,80}\n)", text or "")]
    out: list[str] = []
    buf = ""
    for p in paras:
        if not p:
            continue
        while len(p) > size:  # very long paragraph: hard split at a sentence end
            cut = p.rfind(". ", 0, size)
            cut = cut + 1 if cut > size // 2 else size
            if buf:
                out.append(buf)
                buf = ""
            out.append(p[:cut].strip())
            p = p[cut:].strip()
        if len(buf) + len(p) + 1 > size and buf:
            out.append(buf)
            buf = p
        else:
            buf = f"{buf}\n{p}" if buf else p
    if buf:
        out.append(buf)
    return [c for c in out if len(c) > 120]  # drop headings and fragments


_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "by", "with", "as", "at",
    "is", "are", "was", "were", "be", "been", "this", "that", "it", "its", "from", "which",
    "their", "our", "we", "any", "all", "can", "may", "will", "would", "could", "should",
    "has", "have", "had", "not", "no", "but", "if", "than", "then", "so", "such", "these",
    "those", "into", "over", "under", "about"
})


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1]


def bm25(passages: list[dict[str, Any]], query: str, k: int = 5,
         k1: float = 1.5, b: float = 0.75) -> list[dict[str, Any]]:
    """Rank passages (each with a "text") by BM25 relevance to the query."""
    q = set(_tokens(query))
    if not q or not passages:
        return []
    docs = [_tokens(p["text"]) for p in passages]
    avg = sum(len(d) for d in docs) / len(docs) or 1
    df = Counter(t for d in docs for t in set(d) if t in q)
    n = len(docs)
    scored = []
    for p, d in zip(passages, docs, strict=True):
        tf = Counter(d)
        score = sum(
            math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / avg))
            for t in q if tf[t]
        )
        if score > 0:
            scored.append((score, p))
    scored.sort(key=lambda x: -x[0])
    return [{**p, "score": round(s, 3)} for s, p in scored[:k]]


def html_to_text(html: str) -> str:
    """Docling when installed (keeps tables), otherwise a plain tag strip."""
    try:
        import io

        from docling.datamodel.base_models import DocumentStream, InputFormat
        from docling.document_converter import DocumentConverter

        conv = DocumentConverter(allowed_formats=[InputFormat.HTML])
        res = conv.convert(DocumentStream(name="doc.html", stream=io.BytesIO(html.encode())))
        return res.document.export_to_markdown()
    except ImportError:
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;|&#160;", " ", text)
        text = re.sub(r"&amp;", "&", text)
        return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


# ---------------------------------------------------------------- SEC source

def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def _row(df, concept: str) -> Any:
    rows = df[df["standard_concept"] == concept] if "standard_concept" in df else df.iloc[0:0]
    rows = rows[~rows["dimension"]] if "dimension" in rows else rows
    return rows.iloc[0] if len(rows) else None


def _period_columns(df) -> list[str]:
    return [c for c in df.columns if re.match(r"\d{4}-\d{2}-\d{2} \(", str(c))]


class SecSource:
    """Reads SEC filings for one process. `company_factory(symbol)` must return an
    object with get_filings(form=...) like edgar.Company (tests pass fakes)."""

    def __init__(self, identity: str, cache_dir: Path, company_factory=None,
                 news_lookback_days: int = 120):
        if not identity:
            raise SecUnavailable("data.sec_user_agent is not set (the SEC requires a contact "
                                 "identity, e.g. 'Qfii you@example.com')")
        self.cache_dir = cache_dir
        self.news_lookback_days = news_lookback_days
        if company_factory is None:
            try:
                from edgar import Company, set_identity
            except ImportError as exc:
                raise SecUnavailable("EdgarTools is not installed: pip install 'qfii[sec]'") \
                    from exc
            set_identity(identity)
            for noisy in ("httpx", "httpcore"):  # edgar turns request logging back on
                logging.getLogger(noisy).disabled = True
            logging.getLogger("edgar").setLevel(logging.WARNING)
            company_factory = Company
        self._company = company_factory
        self._companies: dict[str, Any] = {}
        self._memo: dict[tuple, Any] = {}   # (kind, symbol, as_of) -> result, this process

    # -------------------------------------------------- filing selection
    def company(self, symbol: str) -> Any:
        if symbol not in self._companies:
            try:
                self._companies[symbol] = self._company(symbol)
            except Exception as exc:
                raise SecUnavailable(f"{symbol}: not an SEC filer ({exc})") from exc
        return self._companies[symbol]

    def filings(self, symbol: str, form: str, as_of: date, limit: int = 1,
                since: date | None = None) -> list[Any]:
        """Most recent filings of `form` filed on or before as_of (newest first).
        Originals are preferred; amendments (e.g. 10-K/A, often only Part III) are
        used only when no original is available."""
        listed = list(self.company(symbol).get_filings(form=form) or [])
        originals = [f for f in listed if str(getattr(f, "form", form)).upper() == form.upper()]
        out = []
        for f in originals or listed:
            filed = f.filing_date if isinstance(f.filing_date, date) else \
                date.fromisoformat(str(f.filing_date)[:10])
            if filed > as_of:
                continue
            if since and filed < since:
                break
            out.append(f)
            if len(out) >= limit:
                break
        return out

    # -------------------------------------------------- cache
    def _cached(self, symbol: str, filing: Any, kind: str, build) -> Any:
        path = self.cache_dir / symbol / f"{filing.accession_no}.{kind}.json"
        if path.exists():
            return json.loads(path.read_text())
        value = build()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, default=str))
        return value

    def _meta(self, symbol: str, f: Any) -> dict[str, Any]:
        # period_of_report / urls can trigger a download of the filing header: cache them
        return self._cached(symbol, f, "meta", lambda: {
            "form": f.form, "filed": str(f.filing_date)[:10],
            "period": str(getattr(f, "period_of_report", "") or "")[:10],
            "accession": f.accession_no,
            "url": getattr(f, "homepage_url", None) or getattr(f, "url", None)})

    def _memoized(self, kind: str, symbol: str, as_of: date, build) -> Any:
        key = (kind, symbol, as_of)
        if key not in self._memo:
            self._memo[key] = build()
        return self._memo[key]

    # -------------------------------------------------- fundamentals
    def _statement_metrics(self, f: Any) -> dict[str, Any]:
        fin = f.obj().financials
        out: dict[str, Any] = {}
        for name, method in (("revenue", "get_revenue"), ("net_income", "get_net_income"),
                             ("operating_income", "get_operating_income"),
                             ("operating_cash_flow", "get_operating_cash_flow"),
                             ("free_cash_flow", "get_free_cash_flow"),
                             ("capital_expenditures", "get_capital_expenditures"),
                             ("total_assets", "get_total_assets"),
                             ("total_liabilities", "get_total_liabilities"),
                             ("stockholders_equity", "get_stockholders_equity"),
                             ("diluted_shares", "get_shares_outstanding_diluted")):
            try:
                v = _num(getattr(fin, method)())
            except Exception:  # noqa: BLE001 - a missing line item is not fatal
                v = None
            if v is not None:
                out[name] = v
        try:  # prior-period values for growth: same period one year earlier
            df = fin.income_statement().to_dataframe()
            cols = _period_columns(df)
            for name, concept in (("revenue", "Revenue"), ("net_income", "NetIncome"),
                                  ("operating_income", "OperatingIncome")):
                row = _row(df, concept)
                if row is not None and len(cols) >= 2:
                    cur, prior = _num(row[cols[0]]), _num(row[cols[1]])
                    if cur is not None and prior:
                        out[f"{name}_prior_period"] = prior
                        out.setdefault(name, cur)
            out["periods"] = cols[:2]
        except Exception as exc:  # noqa: BLE001 - growth is optional; keep the levels
            log.info("no prior-period statement for %s: %s", f.accession_no, exc)
        return out

    def fundamentals(self, symbol: str, as_of: date) -> dict[str, Any]:
        return self._memoized("fundamentals", symbol, as_of,
                              lambda: self._fundamentals(symbol, as_of))

    def _fundamentals(self, symbol: str, as_of: date) -> dict[str, Any]:
        result: dict[str, Any] = {"available": False, "entity": None}
        for form, key in (("10-K", "annual"), ("10-Q", "quarterly")):
            found = self.filings(symbol, form, as_of)
            if not found:
                continue
            f = found[0]
            metrics = self._cached(symbol, f, "metrics", lambda f=f: self._statement_metrics(f))
            result[key] = {**self._meta(symbol, f), "metrics": metrics}
            result["available"] = True
        result["entity"] = getattr(self.company(symbol), "name", symbol)
        derived: dict[str, float] = {}
        for key in ("annual", "quarterly"):
            m = (result.get(key) or {}).get("metrics") or {}
            rev = m.get("revenue")
            if rev and m.get("revenue_prior_period"):
                derived[f"{key}_revenue_growth_yoy"] = round(rev / m["revenue_prior_period"] - 1, 6)
            if rev and m.get("net_income") is not None:
                derived[f"{key}_net_margin"] = round(m["net_income"] / rev, 6)
            if rev and m.get("operating_income") is not None:
                derived[f"{key}_operating_margin"] = round(m["operating_income"] / rev, 6)
            if key == "annual" and rev and m.get("free_cash_flow") is not None:
                derived["annual_free_cash_flow_margin"] = round(m["free_cash_flow"] / rev, 6)
            if key == "annual" and m.get("stockholders_equity") and m.get("total_liabilities"):
                derived["liabilities_to_equity"] = round(
                    m["total_liabilities"] / m["stockholders_equity"], 6)
        result["derived"] = derived
        return result

    # -------------------------------------------------- news (8-K)
    def _eight_k(self, f: Any) -> dict[str, Any]:
        obj = f.obj()
        items = [str(i).replace("Item ", "").strip() for i in (getattr(obj, "items", None) or [])]
        text = ""
        for att in getattr(f, "attachments", None) or []:
            if str(getattr(att, "document_type", "")).upper().startswith("EX-99"):
                raw = att.download()
                html = raw.decode(errors="ignore") if isinstance(raw, bytes) else str(raw)
                text = html_to_text(html)
                break
        if not text:
            try:
                text = obj.text() if callable(getattr(obj, "text", None)) else ""
            except Exception:  # noqa: BLE001
                text = ""
        headline = next((ln.strip(" #*") for ln in text.splitlines() if len(ln.strip()) > 20), "")
        return {"items": [{"code": i, "label": ITEM_LABELS.get(i, "other")} for i in items],
                "headline": headline[:300], "text": text[:20000]}

    def news(self, symbol: str, as_of: date, limit: int = 6) -> list[dict[str, Any]]:
        return self._memoized("news", symbol, as_of, lambda: self._news(symbol, as_of, limit))

    def _news(self, symbol: str, as_of: date, limit: int) -> list[dict[str, Any]]:
        since = as_of - timedelta(days=self.news_lookback_days)
        out = []
        for f in self.filings(symbol, "8-K", as_of, limit=limit, since=since):
            data = self._cached(symbol, f, "8k", lambda f=f: self._eight_k(f))
            codes = {i["code"] for i in data["items"]}
            if codes <= {"9.01"}:
                continue  # exhibits only, nothing new
            out.append({**self._meta(symbol, f), **data})
        return out

    # -------------------------------------------------- passages + search
    def _sections(self, f: Any) -> list[dict[str, Any]]:
        obj = f.obj()
        found = []
        if f.form == "10-K":
            for section, attr in (("risk factors", "risk_factors"),
                                  ("management discussion and analysis", "management_discussion")):
                found.append((section, getattr(obj, attr, None) or ""))
        else:  # 10-Q: MD&A is Part I, Item 2
            text = ""
            for getter in (lambda: obj.get_item_with_part("Part I", "Item 2"),
                           lambda: obj["Item 2"]):
                try:
                    text = str(getter() or "")
                except Exception:  # noqa: BLE001
                    text = ""
                if text:
                    break
            found.append(("management discussion and analysis", text))
        return [{"section": s, "text": c} for s, t in found for c in chunk(t)]

    def passages(self, symbol: str, as_of: date) -> list[dict[str, Any]]:
        return self._memoized("passages", symbol, as_of, lambda: self._passages(symbol, as_of))

    def _passages(self, symbol: str, as_of: date) -> list[dict[str, Any]]:
        out = []
        for form in ("10-K", "10-Q"):
            for f in self.filings(symbol, form, as_of):
                for p in self._cached(symbol, f, "passages", lambda f=f: self._sections(f)):
                    out.append({**self._meta(symbol, f), **p})
        for n in self.news(symbol, as_of):
            for c in chunk(n["text"]):
                out.append({**{k: n[k] for k in ("form", "filed", "period", "accession", "url")},
                            "section": "press release", "text": c})
        return out

    def search(self, symbol: str, as_of: date, query: str, k: int = 5) -> list[dict[str, Any]]:
        return bm25(self.passages(symbol, as_of), query, k)
