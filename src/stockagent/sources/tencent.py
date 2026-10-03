"""腾讯财经：东方财富不可用时的行情备用源（三市场日线、指数、简要快照）。"""

from __future__ import annotations

import requests

from ..utils import to_float

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/{kind}/get"
QUOTE_URL = "https://qt.gtimg.cn/q={symbol}"

# 东方财富 secid 前缀 100/124 下的指数 → 腾讯代码
INDEX_MAP = {"HSI": "hkHSI", "HSTECH": "hkHSTECH", "HSCEI": "hkHSCEI", "SPX": "us.INX", "NDX": "us.NDX", "DJIA": "us.DJI"}


def symbol_for(secid: str) -> str | None:
    prefix, _, code = secid.partition(".")
    if prefix == "116":
        return f"hk{code}"
    if prefix in ("100", "124"):
        return INDEX_MAP.get(code)
    if prefix == "1":
        return f"sh{code}"
    if prefix == "0":
        return f"{'bj' if code.startswith(('4', '8', '92')) else 'sz'}{code}"
    return {"105": f"us{code}.OQ", "106": f"us{code}.N", "107": f"us{code}.AM"}.get(prefix)


def fetch_klines(session: requests.Session, secid: str, limit: int = 400) -> list[dict]:
    sym = symbol_for(secid)
    if sym is None:
        return []
    kind = "hkfqkline" if sym.startswith("hk") else "usfqkline" if sym.startswith("us") else "fqkline"
    r = session.get(KLINE_URL.format(kind=kind), params={"param": f"{sym},day,,,{limit},qfq"}, timeout=20)
    r.raise_for_status()
    node = ((r.json() or {}).get("data") or {}).get(sym) or {}
    rows = node.get("qfqday") or node.get("day") or []
    bars = []
    for f in rows:
        bars.append({
            "date": f[0],
            "open": float(f[1]), "close": float(f[2]), "high": float(f[3]), "low": float(f[4]),
            "volume": float(f[5]),
            "amount": None,
            "turnover": to_float(f[7]) / 100 if len(f) > 7 and to_float(f[7]) is not None else None,
        })
    return bars


def fetch_quote(session: requests.Session, secid: str) -> dict | None:
    """简要快照：价格、涨跌、总市值；A 股另有 PE。字段位置因市场而异，只取已核对过的。"""
    sym = symbol_for(secid)
    if sym is None:
        return None
    r = session.get(QUOTE_URL.format(symbol=sym), timeout=15)
    r.raise_for_status()
    text = r.content.decode("gbk", errors="replace")
    if '="' not in text:
        return None
    f = text.split('="', 1)[1].rstrip('";\n').split("~")
    if len(f) < 46:
        return None
    out = {
        "name": f[1], "code": f[2], "price": to_float(f[3]), "prev_close": to_float(f[4]),
        "change_pct": to_float(f[32]) / 100 if to_float(f[32]) is not None else None,
        "market_cap": to_float(f[45]) * 1e8 if to_float(f[45]) is not None else None,
        "source": "tencent",
    }
    if sym.startswith(("sh", "sz", "bj")) and len(f) > 53:
        out.update(pe_ttm=to_float(f[39]), pe_dynamic=to_float(f[52]), pe_static=to_float(f[53]))
    if sym.startswith("hk") and len(f) > 60:  # 每手股数（已核对 00700=100、00005=400、00939=1000、02800=500）
        out["lot"] = to_float(f[60])
    return out
