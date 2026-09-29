"""Google News RSS 检索（简中 / 繁中香港 / 英文）。"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

from ..utils import clean_text

LOCALES = {
    "zh-CN": ("zh-CN", "CN", "CN:zh-Hans"),
    "zh-HK": ("zh-HK", "HK", "HK:zh-Hant"),
    "en-US": ("en-US", "US", "US:en"),
}


def search(session: requests.Session, query: str, locale: str, days: int = 7) -> list[dict]:
    hl, gl, ceid = LOCALES[locale]
    url = f"https://news.google.com/rss/search?q={quote(f'{query} when:{days}d')}&hl={hl}&gl={gl}&ceid={ceid}"
    r = session.get(url, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.findall("./channel/item"):
        title = clean_text(it.findtext("title"))
        media = clean_text(it.findtext("source")) or ""
        if media and title.endswith(f" - {media}"):
            title = title[: -len(media) - 3].strip()
        pub = it.findtext("pubDate")
        if not title or not pub:
            continue
        out.append({
            "source_id": it.findtext("guid") or it.findtext("link"),
            "title": title,
            "snippet": "",
            "url": it.findtext("link"),
            "media": media or "Google News",
            "published_at": parsedate_to_datetime(pub),
        })
    return out
