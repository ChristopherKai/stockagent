"""Nasdaq 分析师数据（美股）：一致目标价与评级分布、年度 EPS 一致预期。无需 key。"""

from __future__ import annotations

import requests

from ..utils import to_float

BASE = "https://api.nasdaq.com/api/analyst/{ticker}/{path}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/124.0 Safari/537.36", "Accept": "application/json"}


def _get(session: requests.Session, ticker: str, path: str) -> dict:
    r = session.get(BASE.format(ticker=ticker.upper(), path=path), headers=HEADERS, timeout=20)
    r.raise_for_status()
    return (r.json() or {}).get("data") or {}


def consensus(session: requests.Session, ticker: str) -> dict | None:
    """{"target": {avg, high, low}, "ratings": {buy, hold, sell}, "eps": [{year, avg, high, low, n, up, down}], "brokers": n}"""
    tp = _get(session, ticker, "targetprice")
    o = tp.get("consensusOverview") or {}
    eps_rows = ((_get(session, ticker, "earnings-forecast").get("yearlyForecast") or {}).get("rows")) or []
    if not o and not eps_rows:
        return None
    ratings = _get(session, ticker, "ratings")
    return {
        "target": {"avg": to_float(o.get("priceTarget")), "high": to_float(o.get("highPriceTarget")), "low": to_float(o.get("lowPriceTarget"))},
        "ratings": {k: int(o.get(k) or 0) for k in ("buy", "hold", "sell")},
        "mean_rating": ratings.get("meanRatingType") or "",
        "brokers": len(ratings.get("brokerNames") or []),
        "eps": [{"year": r.get("fiscalEnd"), "avg": to_float(r.get("consensusEPSForecast")), "high": to_float(r.get("highEPSForecast")),
                 "low": to_float(r.get("lowEPSForecast")), "n": r.get("noOfEstimates"), "up": r.get("up"), "down": r.get("down")}
                for r in eps_rows],
    }
