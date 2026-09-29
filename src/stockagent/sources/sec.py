"""美国 SEC EDGAR：8-K / 10-Q / 10-K 等申报文件。SEC 要求 User-Agent 带联系方式。"""

from __future__ import annotations

from datetime import datetime, timezone

import requests

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"


def lookup_cik(session: requests.Session, ticker: str, user_agent: str) -> int | None:
    r = session.get(TICKERS_URL, headers={"User-Agent": user_agent}, timeout=30)
    r.raise_for_status()
    t = ticker.upper()
    for row in (r.json() or {}).values():
        if row.get("ticker", "").upper() == t:
            return int(row["cik_str"])
    return None


def filings(session: requests.Session, cik: int, user_agent: str, start: datetime, forms: set[str]) -> list[dict]:
    r = session.get(SUBMISSIONS_URL.format(cik=cik), headers={"User-Agent": user_agent}, timeout=30)
    r.raise_for_status()
    recent = (r.json() or {}).get("filings", {}).get("recent", {})
    out = []
    for i, form in enumerate(recent.get("form", [])):
        if form not in forms:
            continue
        accepted = recent["acceptanceDateTime"][i]
        published = datetime.fromisoformat(accepted.replace("Z", "+00:00")).astimezone(timezone.utc)
        if published < start:
            continue
        acc = recent["accessionNumber"][i]
        doc = recent["primaryDocument"][i]
        desc = recent.get("primaryDocDescription", [""] * (i + 1))[i] or ""
        items = recent.get("items", [""] * (i + 1))[i] or ""
        title = f"{form} {desc}".strip() + (f"（8-K items: {items}）" if items else "")
        out.append({
            "source_id": acc,
            "title": title,
            "category": form,
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}",
            "published_at": published,
        })
    return out
