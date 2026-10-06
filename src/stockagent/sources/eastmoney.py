"""东方财富：三市场日 K 线、行情快照、个股新闻检索。"""

from __future__ import annotations

import json
from datetime import datetime

import requests

from ..config import Listing
from ..utils import CST, clean_text, jsonp_body, log, to_float

KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
SEARCH_URL = "https://search-api-web.eastmoney.com/search/jsonp"
SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"
SUGGEST_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"  # 东方财富网页搜索框使用的公开 token

US_MARKETS = {"NASDAQ": "105", "NYSE": "106", "AMEX": "107"}

# 快照字段（fltt=2 时为已换算的浮点数）
QUOTE_FIELDS = {
    "f43": "price", "f44": "high", "f45": "low", "f46": "open", "f47": "volume", "f48": "amount",
    "f57": "code", "f58": "name", "f60": "prev_close", "f116": "market_cap", "f117": "float_market_cap",
    "f162": "pe_dynamic", "f163": "pe_static", "f164": "pe_ttm", "f167": "pb", "f168": "turnover_rate",
    "f170": "change_pct", "f171": "amplitude", "f189": "listing_date",
}


def secid_for(listing: Listing) -> str:
    if listing.market == "HK":
        return f"116.{listing.code}"
    if listing.market == "A":
        return f"{1 if listing.exchange == 'SH' else 0}.{listing.code}"
    if listing.exchange in US_MARKETS:
        return f"{US_MARKETS[listing.exchange]}.{listing.code}"
    # 没填交易所的美股由 app.us_secid 识别；这里只给出缺省值
    log.warning("美股 %s 没有 exchange，暂按纳斯达克处理；请在 watchlist.yaml 中为它填写 exchange", listing.code)
    return f"105.{listing.code}"


def fetch_klines(session: requests.Session, secid: str, limit: int = 400, adjust: int = 1) -> list[dict]:
    """日 K 线。adjust: 0 不复权 / 1 前复权 / 2 后复权。"""
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": str(adjust),
        "lmt": str(limit),
        "end": "20500101",
    }
    r = session.get(KLINE_URL, params=params, timeout=20)
    r.raise_for_status()
    data = (r.json() or {}).get("data")
    if not data:
        return []
    bars = []
    for line in data.get("klines") or []:
        f = line.split(",")
        bars.append({
            "date": f[0],
            "open": float(f[1]), "close": float(f[2]), "high": float(f[3]), "low": float(f[4]),
            "volume": float(f[5]), "amount": float(f[6]),
            "turnover": to_float(f[10]) if len(f) > 10 else None,
        })
    return bars


def fetch_quote(session: requests.Session, secid: str) -> dict | None:
    params = {"secid": secid, "fltt": "2", "invt": "2", "fields": ",".join(QUOTE_FIELDS)}
    r = session.get(QUOTE_URL, params=params, timeout=15)
    r.raise_for_status()
    data = (r.json() or {}).get("data")
    if not data:
        return None
    out = {}
    for k, name in QUOTE_FIELDS.items():
        v = data.get(k)
        out[name] = v if name in ("code", "name") else to_float(v)
    if out.get("listing_date"):
        out["listing_date"] = str(int(out["listing_date"]))
    # 百分比统一成小数
    for k in ("change_pct", "amplitude", "turnover_rate"):
        if out.get(k) is not None:
            out[k] = out[k] / 100
    return out


def search_news(session: requests.Session, keyword: str, page_size: int = 20) -> list[dict]:
    """个股新闻（东方财富资讯搜索，按时间倒序）。"""
    param = {
        "uid": "",
        "keyword": keyword,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default", "sort": "time", "pageIndex": 1,
                "pageSize": page_size, "preTag": "", "postTag": "",
            }
        },
    }
    r = session.get(
        SEARCH_URL,
        params={"cb": "cb", "param": json.dumps(param, ensure_ascii=False)},
        headers={"Referer": "https://so.eastmoney.com/"},
        timeout=20,
    )
    r.raise_for_status()
    body = jsonp_body(r.text)
    rows = (body.get("result") or {}).get("cmsArticleWebOld") or []
    out = []
    for x in rows:
        try:
            published = datetime.strptime(x["date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        except (KeyError, ValueError):
            log.debug("东方财富新闻时间无法解析：%s", x.get("date"))
            continue
        out.append({
            "source_id": x.get("code") or x.get("url"),
            "title": clean_text(x.get("title")),
            "snippet": clean_text(x.get("content"))[:300],
            "url": x.get("url"),
            "media": x.get("mediaName") or "东方财富",
            "published_at": published,
        })
    return out


def suggest(session: requests.Session, query: str, count: int = 10) -> list[dict]:
    """股票名称或代码联想（东方财富搜索框）：[{code, name, mkt_num, type}]，含窝轮、ETF、期货等，由调用方筛选。"""
    r = session.get(SUGGEST_URL, params={"input": query, "type": "14", "token": SUGGEST_TOKEN, "count": str(count)}, timeout=15)
    r.raise_for_status()
    rows = ((r.json() or {}).get("QuotationCodeTable") or {}).get("Data") or []
    return [{"code": str(x.get("Code") or ""), "name": str(x.get("Name") or ""), "mkt_num": str(x.get("MktNum") or ""),
             "type": str(x.get("SecurityTypeName") or "")} for x in rows]
