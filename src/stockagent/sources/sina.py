"""新浪财经美股：东方财富、腾讯都不可用时的备用行情源（只覆盖美股：日线与含总市值的快照）。"""

from __future__ import annotations

import json
import re

import requests

from ..utils import to_float

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
           "Referer": "https://finance.sina.com.cn"}
KLINE_URL = "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var%20_x=/US_MinKService.getDailyK"
QUOTE_URL = "https://hq.sinajs.cn/list=gb_{code}"


def ticker_for(secid: str) -> str | None:
    prefix, _, code = secid.partition(".")
    return code.lower().replace(".", "$") if prefix in ("105", "106", "107") and re.fullmatch(r"[A-Za-z][A-Za-z.\-]{0,7}", code) else None


def fetch_klines(session: requests.Session, secid: str, limit: int = 400) -> list[dict]:
    t = ticker_for(secid)
    if t is None:
        return []
    r = session.get(KLINE_URL, params={"symbol": t.upper(), "type": "daily"}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    m = re.search(r"=\((\[.*\])\)", r.text, re.S)
    rows = json.loads(m.group(1)) if m else []
    return [{"date": x["d"], "open": float(x["o"]), "close": float(x["c"]), "high": float(x["h"]), "low": float(x["l"]),
             "volume": float(x["v"]), "amount": to_float(x.get("a")), "turnover": None} for x in rows[-limit:]]


def fetch_quote(session: requests.Session, secid: str) -> dict | None:
    """快照：价格、昨收、总市值（美元）、股本（字段位置已按 BE 的实际返回核对）。"""
    t = ticker_for(secid)
    if t is None:
        return None
    r = session.get(QUOTE_URL.format(code=t), headers=HEADERS, timeout=15)
    r.raise_for_status()
    text = r.content.decode("gbk", errors="replace")
    if '="' not in text:
        return None
    f = text.split('="', 1)[1].rstrip('";\n').split(",")
    if len(f) < 27 or not to_float(f[1]):
        return None
    return {"name": f[0], "code": t.upper(), "price": to_float(f[1]), "change_pct": (to_float(f[2]) or 0) / 100,
            "prev_close": to_float(f[26]), "market_cap": to_float(f[12]), "shares": to_float(f[19]), "source": "sina"}
