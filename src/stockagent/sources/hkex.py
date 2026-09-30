"""港交所披露易（HKEXnews）：公告与定期报告。"""

from __future__ import annotations

import json
import re
from datetime import datetime

import requests

from ..utils import CST, clean_text, jsonp_body

BASE = "https://www1.hkexnews.hk"

# 标题类别：40000 财务报表/ESG；40100 年报；40200 中期/半年度报告
REPORT_T2 = {"annual_report": "40100", "interim_report": "40200"}


def lookup_stock_id(session: requests.Session, code: str) -> int | None:
    r = session.get(
        f"{BASE}/search/prefix.do",
        params={"callback": "callback", "lang": "ZH", "type": "A", "name": code, "market": "SEHK"},
        timeout=20,
    )
    r.raise_for_status()
    for s in jsonp_body(r.text).get("stockInfo") or []:
        if s.get("code") == code:
            return int(s["stockId"])
    return None


def _search(session: requests.Session, stock_id: int, start: datetime, end: datetime,
            t1code: str = "-2", t2code: str = "-2", rows: int = 100, lang: str = "zh") -> list[dict]:
    params = {
        "sortDir": "0", "sortByOptions": "DateTime", "category": "0", "market": "SEHK",
        "stockId": str(stock_id), "documentType": "-1",
        "fromDate": start.astimezone(CST).strftime("%Y%m%d"), "toDate": end.astimezone(CST).strftime("%Y%m%d"),
        "title": "", "searchType": "0" if t1code == "-2" else "1",
        "t1code": t1code, "t2Gcode": "-2", "t2code": t2code, "rowRange": str(rows), "lang": lang,
    }
    r = session.get(f"{BASE}/search/titleSearchServlet.do", params=params, timeout=30)
    r.raise_for_status()
    result = (r.json() or {}).get("result")
    if not result or result == "null":
        return []
    out = []
    for x in json.loads(result):
        published = datetime.strptime(x["DATE_TIME"], "%d/%m/%Y %H:%M").replace(tzinfo=CST)
        out.append({
            "source_id": str(x["NEWS_ID"]),
            "title": clean_text(x.get("TITLE")),
            "category": clean_text(x.get("LONG_TEXT")),
            "url": BASE + x["FILE_LINK"] if x.get("FILE_LINK", "").startswith("/") else x.get("FILE_LINK"),
            "file_type": x.get("FILE_TYPE"),
            "size": x.get("FILE_INFO"),
            "published_at": published,
        })
    return out


def announcements(session: requests.Session, stock_id: int, start: datetime, end: datetime) -> list[dict]:
    return _search(session, stock_id, start, end)


def reports(session: requests.Session, stock_id: int, start: datetime, end: datetime) -> list[dict]:
    """年报与中期报告，按发布时间倒序。"""
    out = []
    for doc_type, t2 in REPORT_T2.items():
        for x in _search(session, stock_id, start, end, t1code="40000", t2code=t2, rows=20):
            if (x.get("file_type") or "").upper() != "PDF":
                continue
            x["doc_type"] = doc_type
            out.append(x)
    return sorted(out, key=lambda x: x["published_at"], reverse=True)


def market_announcements(session: requests.Session, day: datetime, rows: int = 5000) -> list[dict]:
    """某一天全市场的公告标题（选股器的事件扫描用）：每条带股票代码（一条公告可能对应多只股票）。"""
    params = {
        "sortDir": "0", "sortByOptions": "DateTime", "category": "0", "market": "SEHK", "stockId": "-1", "documentType": "-1",
        "fromDate": day.astimezone(CST).strftime("%Y%m%d"), "toDate": day.astimezone(CST).strftime("%Y%m%d"),
        "title": "", "searchType": "0", "t1code": "-2", "t2Gcode": "-2", "t2code": "-2", "rowRange": str(rows), "lang": "zh",
    }
    r = session.get(f"{BASE}/search/titleSearchServlet.do", params=params, timeout=60)
    r.raise_for_status()
    result = (r.json() or {}).get("result")
    if not result or result == "null":
        return []
    out = []
    for x in json.loads(result):
        codes = [c.strip() for c in re.split(r"<br\s*/?>", x.get("STOCK_CODE") or "") if c.strip()]
        names = [n.strip() for n in re.split(r"<br\s*/?>", x.get("STOCK_NAME") or "")]
        out.append({
            "source_id": str(x["NEWS_ID"]),
            "codes": codes, "names": names,
            "title": clean_text(x.get("TITLE")),
            "category": clean_text(x.get("LONG_TEXT")),
            "url": BASE + x["FILE_LINK"] if x.get("FILE_LINK", "").startswith("/") else x.get("FILE_LINK"),
            "published_at": datetime.strptime(x["DATE_TIME"], "%d/%m/%Y %H:%M").replace(tzinfo=CST),
        })
    return out
