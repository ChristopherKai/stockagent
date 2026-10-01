"""Google 新闻 RSS 的 pubDate 是聚合器的收录/更新时间，不是文章发布时间（聚合页、时间线页会被反复更新）。
这里换出原文地址，从页面里读真实发布时间。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

from ..utils import CST, utcnow
from . import web

_DATE = re.compile(
    r"(20\d\d)[-/.年](\d{1,2})[-/.月](\d{1,2})日?(?:[T\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?\s*(Z|[+-]\d{2}:?\d{2})?")
_DATE_CLASS = re.compile(r"(date|time|pub)", re.I)
_META = ("article:published_time", "og:article:published_time", "datepublished", "publishdate", "pubdate",
         "publish_date", "publishtime", "date", "dc.date", "dc.date.issued")


def parse_date(s: str) -> datetime | None:
    """解析常见页面日期写法；没有时区的按北京时间。"""
    m = _DATE.search(s or "")
    if not m:
        return None
    y, mo, d, h, mi, sec, tz = m.groups()
    try:
        if tz and tz != "Z":
            sign = -1 if tz[0] == "-" else 1
            digits = tz[1:].replace(":", "")
            zone = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
        else:
            zone = timezone.utc if tz == "Z" else CST
        return datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(sec or 0), tzinfo=zone)
    except ValueError:
        return None


def _json_ld_dates(soup: BeautifulSoup) -> list[str]:
    out: list[str] = []

    def walk(o) -> None:
        if isinstance(o, dict):
            if isinstance(o.get("datePublished"), str):
                out.append(o["datePublished"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(tag.string or ""))
        except ValueError:
            continue
    return out


def extract_date(html: str) -> datetime | None:
    """优先读 meta / JSON-LD 里声明的发布时间，其次取页面里第一个 <time>。"""
    soup = BeautifulSoup(html, "html.parser")
    cands = [m.get("content", "") for m in soup.find_all("meta")
             if (m.get("property") or m.get("name") or m.get("itemprop") or "").lower() in _META]
    cands += [t.get("content", "") for t in soup.find_all(attrs={"itemprop": "datePublished"})]
    cands += _json_ld_dates(soup)
    cands += re.findall(r'"?datePublished"?\s*[:=]\s*"?([^",}<]{8,40})', html)  # JSON-LD 写坏了（引号缺失等）时的兜底
    cands += [t.get("datetime") or t.get_text() for t in soup.find_all("time")[:1]]
    cands += re.findall(r"(?:pagepubtime|ctime)\s*[:=]\s*['\"]?([^'\",;]{8,25})", html)  # 新浪等站点
    for tag in soup.find_all(class_=_DATE_CLASS)[:6]:  # 页面里写着「发布时间」的元素：只认短文本
        if len(t := tag.get_text(" ", strip=True)) <= 40:
            cands.append(t)
    for c in cands:
        if d := parse_date(c):
            return d
    return None


def real_published(link: str, listed: datetime) -> datetime | None:
    """返回原文页面声明的发布时间；无法解析、或晚于 RSS 时间（不可能，多半读到了页面生成时间）时返回 None。"""
    real = web._gnews_url(link)
    if not real or web._is_private(real):
        return None
    r = web._http.get(real, timeout=web.FETCH_TIMEOUT)
    r.raise_for_status()
    if web._is_private(r.url):
        return None
    r.encoding = r.apparent_encoding if not r.encoding or r.encoding.lower() == "iso-8859-1" else r.encoding
    d = extract_date(r.text)
    if d is None or d > min(listed, utcnow()) + timedelta(hours=1):
        return None
    return d
