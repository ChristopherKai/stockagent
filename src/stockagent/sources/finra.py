"""FINRA 空头数据（美股，所有交易所，无需 key）：双周「做空持仓」文件与每日「做空成交量」文件。
Nasdaq 的 short-interest 接口只支持 Nasdaq 上市股票，NYSE / AMEX 上市的要靠这里。"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta

import requests

from ..utils import to_float

SI_URL = "https://cdn.finra.org/equity/otcmarket/biweekly/shrt{day}.csv"
VOL_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{day}.txt"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def settlement_dates(today: date, n: int = 8) -> list[date]:
    """最近的做空持仓结算日（每月 15 日、月末，遇周末顺延到前一个工作日），新的在前；文件在结算日后约一周发布。"""
    out, d = [], today
    while len(out) < n and (today - d).days < 120:
        for day in (15, None):
            c = date(d.year, d.month, day) if day else (date(d.year + (d.month == 12), d.month % 12 + 1, 1) - timedelta(days=1))
            while c.weekday() >= 5:
                c -= timedelta(days=1)
            if c <= today and c not in out:
                out.append(c)
        d = (d.replace(day=1) - timedelta(days=1))
    return sorted(out, reverse=True)[:n]


def _row(text: str, symbol: str) -> dict | None:
    for r in csv.DictReader(io.StringIO(text), delimiter="|"):
        if (r.get("symbolCode") or "").upper() == symbol.upper():
            return {"date": r["settlementDate"], "short": to_float(r["currentShortPositionQuantity"]),
                    "prev": to_float(r["previousShortPositionQuantity"]), "avg_volume": to_float(r["averageDailyVolumeQuantity"]),
                    "days_to_cover": to_float(r["daysToCoverQuantity"]), "change_pct": to_float(r["changePercent"])}
    return None


def short_interest_file(session: requests.Session, day: date, symbol: str) -> dict | None | bool:
    """某结算日的该股做空持仓；文件还没发布返回 False，已发布但没有该股返回 None。"""
    r = session.get(SI_URL.format(day=day.strftime("%Y%m%d")), headers=HEADERS, timeout=60)
    if r.status_code in (403, 404):
        return False
    r.raise_for_status()
    return _row(r.text, symbol)


def daily_short_volume(session: requests.Session, day: date, symbol: str) -> dict | None | bool:
    """某交易日的卖空成交量与总成交量（占比≠做空持仓，做市商对冲也计入）；非交易日 / 未发布返回 False。"""
    r = session.get(VOL_URL.format(day=day.strftime("%Y%m%d")), headers=HEADERS, timeout=60)
    if r.status_code in (403, 404):
        return False
    r.raise_for_status()
    for line in r.text.splitlines()[1:]:
        f = line.split("|")
        if len(f) >= 5 and f[1].upper() == symbol.upper():
            short, total = to_float(f[2]), to_float(f[4])
            return {"date": f[0], "short_volume": short, "total_volume": total, "ratio": short / total if short and total else None}
    return None
