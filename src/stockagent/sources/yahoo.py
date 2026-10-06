"""Yahoo Finance 美股（chart 接口，无需密钥）：日线、简要快照，以及按代码识别交易所。

只作美股的兜底行情源：日线是拆股复权（不含分红复权），快照没有总市值；
chart 的 meta 带交易所名，是识别美股代码属于纳斯达克 / 纽交所 / 美交所最省事的办法。
Yahoo 对部分网络会返回 429，所以不放在默认行情源里，由 sources.price_sources 显式加入。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import requests

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

# Yahoo exchangeName → 东方财富 secid 前缀
EXCHANGE_PREFIX = {"NMS": "105", "NGM": "105", "NCM": "105", "NYQ": "106", "ASE": "107", "PCX": "107", "NYS": "106"}


def symbol_for(secid: str) -> str | None:
    prefix, _, code = secid.partition(".")
    if prefix in ("105", "106", "107") and re.fullmatch(r"[A-Za-z][A-Za-z.\-]{0,7}", code):
        return code.upper().replace(".", "-")  # BRK.B → BRK-B
    return None


def _range_for(limit: int) -> str:
    for n, r in ((20, "1mo"), (120, "6mo"), (250, "1y"), (500, "2y"), (1250, "5y")):
        if limit <= n:
            return r
    return "10y"


def _chart(session: requests.Session, symbol: str, rng: str) -> dict | None:
    r = session.get(CHART_URL.format(symbol=symbol), params={"range": rng, "interval": "1d"}, headers=HEADERS, timeout=20)
    if r.status_code == 404:  # 代码不存在
        return None
    r.raise_for_status()
    res = ((r.json() or {}).get("chart") or {}).get("result") or []
    return res[0] if res else None


def _bars(node: dict) -> list[dict]:
    q = ((node.get("indicators") or {}).get("quote") or [{}])[0]
    offset = (node.get("meta") or {}).get("gmtoffset") or 0  # 按交易所本地时间取日期
    bars = []
    for i, ts in enumerate(node.get("timestamp") or []):
        o, h, l, c, v = (q.get(k, [None] * (i + 1))[i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c):  # 停牌 / 缺值的日子 Yahoo 给 null
            continue
        d = (datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(seconds=offset)).date().isoformat()
        bars.append({"date": d, "open": float(o), "close": float(c), "high": float(h), "low": float(l),
                     "volume": float(v or 0), "amount": None, "turnover": None})
    return bars


def fetch_klines(session: requests.Session, secid: str, limit: int = 400) -> list[dict]:
    sym = symbol_for(secid)
    if sym is None:
        return []
    node = _chart(session, sym, _range_for(limit))
    return _bars(node)[-limit:] if node else []


def fetch_quote(session: requests.Session, secid: str) -> dict | None:
    """价格与涨跌幅（由最近两根日线算；没有总市值，排在腾讯、新浪之后用）。"""
    sym = symbol_for(secid)
    if sym is None:
        return None
    node = _chart(session, sym, "5d")
    bars = _bars(node) if node else []
    if not bars:
        return None
    meta = node["meta"]
    price = meta.get("regularMarketPrice") or bars[-1]["close"]
    prev = bars[-2]["close"] if len(bars) > 1 else None
    return {"name": meta.get("longName") or meta.get("shortName") or sym, "code": sym, "price": price, "prev_close": prev,
            "change_pct": (price / prev - 1) if prev else None, "market_cap": None, "source": "yahoo"}


def exchange_prefix(session: requests.Session, code: str) -> str | None:
    """美股代码 → 东方财富交易所前缀（105 纳斯达克 / 106 纽交所 / 107 美交所）；Yahoo 不认识或在场外市场返回 None。"""
    sym = symbol_for(f"105.{code}")
    if sym is None:
        return None
    node = _chart(session, sym, "5d")
    return EXCHANGE_PREFIX.get(((node or {}).get("meta") or {}).get("exchangeName") or "")
