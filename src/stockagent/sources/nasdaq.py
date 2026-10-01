"""Nasdaq 分析师数据（美股）：一致目标价与评级分布、年度 EPS 一致预期、季度业绩惊喜（实际 vs 当时的一致预期）。无需 key。"""

from __future__ import annotations

import re
from datetime import datetime

import requests

from ..utils import to_float

BASE = "https://api.nasdaq.com/api/{kind}/{ticker}/{path}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/124.0 Safari/537.36", "Accept": "application/json"}


def _get(session: requests.Session, ticker: str, path: str, kind: str = "analyst") -> dict:
    r = session.get(BASE.format(kind=kind, ticker=ticker.upper(), path=path), headers=HEADERS, timeout=20)
    r.raise_for_status()
    return (r.json() or {}).get("data") or {}


def _fy(label: str | None) -> int | None:
    """「Dec 2026」→ 2026（财年截止所在年份）。"""
    m = re.search(r"(\d{4})", label or "")
    return int(m.group(1)) if m else None


def yearly_eps(session: requests.Session, ticker: str) -> list[dict]:
    """年度 EPS 一致预期（中概股为每 ADS 美元口径）：[{year, fy, avg, high, low, n, up, down}]。"""
    rows = ((_get(session, ticker, "earnings-forecast").get("yearlyForecast") or {}).get("rows")) or []
    return [{"year": r.get("fiscalEnd"), "fy": _fy(r.get("fiscalEnd")), "avg": to_float(r.get("consensusEPSForecast")),
             "high": to_float(r.get("highEPSForecast")), "low": to_float(r.get("lowEPSForecast")),
             "n": r.get("noOfEstimates"), "up": r.get("up"), "down": r.get("down")}
            for r in rows]


def consensus(session: requests.Session, ticker: str) -> dict | None:
    """{"target": {avg, high, low}, "ratings": {buy, hold, sell}, "eps": [{year, fy, avg, high, low, n, up, down}], "brokers": n}"""
    tp = _get(session, ticker, "targetprice")
    o = tp.get("consensusOverview") or {}
    eps_rows = yearly_eps(session, ticker)
    if not o and not eps_rows:
        return None
    ratings = _get(session, ticker, "ratings")
    return {
        "target": {"avg": to_float(o.get("priceTarget")), "high": to_float(o.get("highPriceTarget")), "low": to_float(o.get("lowPriceTarget"))},
        "ratings": {k: int(o.get(k) or 0) for k in ("buy", "hold", "sell")},
        "mean_rating": ratings.get("meanRatingType") or "",
        "brokers": len(ratings.get("brokerNames") or []),
        "eps": eps_rows,
    }


def earnings_surprise(session: requests.Session, ticker: str) -> list[dict]:
    """近几个季度的实际 EPS 与当时的一致预期（Nasdaq 口径，多为经调整 EPS；中概股常无一致预期，记为 None）。"""
    rows = ((_get(session, ticker, "earnings-surprise", kind="company").get("earningsSurpriseTable") or {}).get("rows")) or []
    out = []
    for r in rows:
        try:
            reported = datetime.strptime(r.get("dateReported") or "", "%m/%d/%Y").date().isoformat()
        except ValueError:
            reported = None
        out.append({"quarter": r.get("fiscalQtrEnd"), "reported": reported, "eps": to_float(r.get("eps")),
                    "consensus": to_float(r.get("consensusForecast")), "surprise_pct": to_float(r.get("percentageSurprise"))})
    return out
