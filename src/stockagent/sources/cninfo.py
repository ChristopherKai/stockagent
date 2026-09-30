"""巨潮资讯：A 股公告与定期报告。"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import requests

from ..config import Listing
from ..utils import CST, clean_text

BASE = "http://www.cninfo.com.cn"
STATIC = "http://static.cninfo.com.cn/"

REPORT_CATEGORY = "category_ndbg_szsh;category_bndbg_szsh;"  # 年报；半年报


def lookup_org_id(session: requests.Session, code: str) -> str | None:
    r = session.post(f"{BASE}/new/information/topSearch/query", data={"keyWord": code, "maxNum": 10}, timeout=20)
    r.raise_for_status()
    for s in r.json() or []:
        if s.get("code") == code and s.get("category") == "A股":
            return s.get("orgId")
    return None


def _column(listing: Listing) -> str:
    return {"SH": "sse", "SZ": "szse", "BJ": "bj"}.get(listing.exchange or "", "szse")


def _query(session: requests.Session, listing: Listing, org_id: str, start: datetime, end: datetime,
           category: str = "", max_pages: int = 5, delay: float = 0.4) -> list[dict]:
    out = []
    se_date = f"{start.astimezone(CST):%Y-%m-%d}~{end.astimezone(CST):%Y-%m-%d}"
    for page in range(1, max_pages + 1):
        data = {
            "pageNum": page, "pageSize": 30, "column": _column(listing), "tabName": "fulltext",
            "plate": "", "stock": f"{listing.code},{org_id}", "searchkey": "", "secid": "",
            "category": category, "trade": "", "seDate": se_date, "sortName": "", "sortType": "",
            "isHLtitle": "false",
        }
        r = session.post(f"{BASE}/new/hisAnnouncement/query", data=data, timeout=30)
        r.raise_for_status()
        body = r.json() or {}
        for x in body.get("announcements") or []:
            out.append({
                "source_id": str(x["announcementId"]),
                "title": clean_text(x.get("announcementTitle")),
                "category": x.get("announcementTypeName") or "",
                "url": STATIC + x["adjunctUrl"] if x.get("adjunctUrl") else None,
                "file_type": x.get("adjunctType"),
                "size": f"{x.get('adjunctSize')}KB" if x.get("adjunctSize") else None,
                "published_at": datetime.fromtimestamp(x["announcementTime"] / 1000, tz=timezone.utc),
            })
        if not body.get("hasMore"):
            break
        time.sleep(delay)
    return out


def announcements(session: requests.Session, listing: Listing, org_id: str, start: datetime, end: datetime) -> list[dict]:
    return _query(session, listing, org_id, start, end)


_SKIP_REPORT = re.compile(r"摘要|英文|已取消|更正前|提示性")


def reports(session: requests.Session, listing: Listing, org_id: str, start: datetime, end: datetime) -> list[dict]:
    """年报与半年报全文（排除摘要、英文版），按发布时间倒序。"""
    out = []
    for x in _query(session, listing, org_id, start, end, category=REPORT_CATEGORY, max_pages=2):
        title = x["title"]
        if _SKIP_REPORT.search(title) or (x.get("file_type") or "").upper() != "PDF":
            continue
        if "半年度报告" in title:
            x["doc_type"] = "interim_report"
        elif "年度报告" in title:
            x["doc_type"] = "annual_report"
        else:
            continue
        out.append(x)
    return sorted(out, key=lambda x: x["published_at"], reverse=True)


def search(session: requests.Session, keyword: str, start: datetime, end: datetime, max_pages: int = 10,
           delay: float = 0.4) -> list[dict]:
    """全市场按标题关键词检索公告（选股器的事件扫描用），返回带代码与简称的公告列表。"""
    out = []
    se_date = f"{start.astimezone(CST):%Y-%m-%d}~{end.astimezone(CST):%Y-%m-%d}"
    for page in range(1, max_pages + 1):
        data = {"pageNum": page, "pageSize": 30, "column": "szse", "tabName": "fulltext", "plate": "", "stock": "",
                "searchkey": keyword, "secid": "", "category": "", "trade": "", "seDate": se_date, "sortName": "",
                "sortType": "", "isHLtitle": "false"}
        r = session.post(f"{BASE}/new/hisAnnouncement/query", data=data, timeout=30)
        r.raise_for_status()
        body = r.json() or {}
        for x in body.get("announcements") or []:
            out.append({
                "source_id": str(x["announcementId"]),
                "code": x.get("secCode"), "name": clean_text(x.get("secName")),
                "title": clean_text(x.get("announcementTitle")),
                "url": STATIC + x["adjunctUrl"] if x.get("adjunctUrl") else None,
                "published_at": datetime.fromtimestamp(x["announcementTime"] / 1000, tz=timezone.utc),
            })
        if not body.get("hasMore"):
            break
        time.sleep(delay)
    return out
