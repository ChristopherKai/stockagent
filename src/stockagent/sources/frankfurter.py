"""Frankfurter（欧洲央行参考汇率）：汇率日线的备用源，用于计算 AH 溢价。"""

from __future__ import annotations

from datetime import date, timedelta

import requests

URL = "https://api.frankfurter.dev/v1/{start}..{end}"

# 东方财富汇率 secid → (基础货币, 报价货币)
FX_MAP = {"HKDCNH": ("HKD", "CNY"), "HKDCNYC": ("HKD", "CNY"), "USDCNH": ("USD", "CNY"), "USDCNYC": ("USD", "CNY")}


def pair_for(secid: str) -> tuple[str, str] | None:
    prefix, _, code = secid.partition(".")
    return FX_MAP.get(code) if prefix in ("133", "120") else None


def fetch_klines(session: requests.Session, secid: str, days: int = 400) -> list[dict]:
    pair = pair_for(secid)
    if pair is None:
        return []
    end = date.today()
    start = end - timedelta(days=days)
    r = session.get(URL.format(start=start, end=end), params={"base": pair[0], "symbols": pair[1]}, timeout=20)
    r.raise_for_status()
    rates = (r.json() or {}).get("rates") or {}
    return [
        {"date": d, "open": v[pair[1]], "close": v[pair[1]], "high": v[pair[1]], "low": v[pair[1]],
         "volume": 0.0, "amount": None, "turnover": None}
        for d, v in sorted(rates.items())
    ]
