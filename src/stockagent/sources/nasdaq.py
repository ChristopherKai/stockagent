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


def _num(v) -> float | None:
    """「(366,261)」「$6,029,783」「-26.41%」→ 数字（括号为负）。"""
    if v is None:
        return None
    t = str(v).strip()
    neg = t.startswith("(") and t.endswith(")")
    x = to_float(re.sub(r"[,$%()\s]", "", t))
    return -x if neg and x is not None else x


def institutions(session: requests.Session, ticker: str, limit: int = 15) -> dict | None:
    """机构持仓（13F 汇总）：{"pct", "holders", "shares", "increased", "decreased", "new", "sold_out", "top": [{name, date, shares, change, change_pct, value_k}]}。"""
    d = _get(session, ticker, f"institutional-holdings?limit={limit}&type=TOTAL&sortColumn=marketValue&sortOrder=DESC", kind="company")
    if not d:
        return None
    rows = ((d.get("holdingsTransactions") or {}).get("table") or {}).get("rows") or []
    pos = {r["positions"]: r for r in (d.get("activePositions") or {}).get("rows") or []}
    ns = {r["positions"]: r for r in (d.get("newSoldOutPositions") or {}).get("rows") or []}
    return {
        "pct": _num(((d.get("ownershipSummary") or {}).get("SharesOutstandingPCT") or {}).get("value")),
        "holders": _num((pos.get("Total Institutional Shares") or {}).get("holders")),
        "shares": _num((pos.get("Total Institutional Shares") or {}).get("shares")),
        "increased": _num((pos.get("Increased Positions") or {}).get("holders")), "decreased": _num((pos.get("Decreased Positions") or {}).get("holders")),
        "new": _num((ns.get("New Positions") or {}).get("holders")), "sold_out": _num((ns.get("Sold Out Positions") or {}).get("holders")),
        "top": [{"name": r["ownerName"], "date": r["date"], "shares": _num(r["sharesHeld"]), "change": _num(r["sharesChange"]),
                 "change_pct": _num(r["sharesChangePCT"]), "value_k": _num(r["marketValue"])} for r in rows],
    }


def insiders(session: requests.Session, ticker: str, limit: int = 20) -> dict | None:
    """内部人交易（Form 4 汇总）：{"summary": [(项目, 近 3 个月, 近 12 个月)], "recent": [{name, relation, date, type, shares, price, held}]}。"""
    d = _get(session, ticker, f"insider-trades?limit={limit}&type=ALL", kind="company")
    if not d:
        return None
    summary = []
    for key in ("numberOfTrades", "numberOfSharesTraded"):
        for r in ((d.get(key) or {}).get("rows") or []):
            summary.append((r.get("insiderTrade"), r.get("months3"), r.get("months12")))
    rows = (((d.get("transactionTable") or {}).get("table")) or {}).get("rows") or []
    return {"summary": summary,
            "recent": [{"name": r["insider"], "relation": r["relation"], "date": r["lastDate"], "type": r["transactionType"],
                        "shares": _num(r["sharesTraded"]), "price": _num(r["lastPrice"]), "held": _num(r["sharesHeld"])} for r in rows]}


def short_interest(session: requests.Session, ticker: str) -> list[dict]:
    """Nasdaq 上市股票的做空持仓（双周）；NYSE / AMEX 上市的接口不支持，返回空（改用 finra）。"""
    rows = ((_get(session, ticker, "short-interest?assetClass=stocks", kind="quote").get("shortInterestTable") or {}).get("rows")) or []
    return [{"date": r["settlementDate"], "short": _num(r["interest"]), "avg_volume": _num(r["avgDailyShareVolume"]),
             "days_to_cover": to_float(r.get("daysToCover"))} for r in rows]
