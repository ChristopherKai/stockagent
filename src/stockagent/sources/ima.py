"""腾讯 ima 知识库（官方 OpenAPI，https://ima.qq.com/agent-interface 获取 Client ID 与 API Key，填进 .env 的 IMA_CLIENT_ID / IMA_API_KEY）。

只用只读接口：列出知识库、按标题检索（只匹配标题，多个词不取交集，最多约 100 条，不按时间排序、不能翻页）、
取原文下载地址（腾讯云 COS 临时链接，需带接口返回的签名头）。知识条目不带发布时间，日期从标题解析。
"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import date
from urllib.parse import urlparse

import requests

from ..utils import today_local

BASE = "https://ima.qq.com/openapi/wiki/v1/"
ENV_CLIENT_ID = "IMA_CLIENT_ID"
ENV_API_KEY = "IMA_API_KEY"
FOLDER = 99
MEDIA_TYPES = {1: "PDF", 2: "笔记", 3: "Word", 4: "PPT", 5: "Excel", 6: "公众号文章", 7: "Markdown", 9: "图片",
               11: "笔记", 12: "AI会话", 13: "TXT", 14: "Xmind", 15: "录音", 16: "视频", 20: "HTML", 21: "EPUB", FOLDER: "文件夹"}
# 可下载后抽取文字的类型 → 文件后缀（图片走本地 OCR；笔记、录音、Excel 等只能看标题）
DOWNLOADABLE = {1: ".pdf", 3: ".docx", 7: ".md", 9: ".png", 13: ".txt", 20: ".html"}
# 原文只从这些域名下载（ima 返回的 COS 临时链接），防止把签名头发到别处
ALLOWED_HOSTS = (".myqcloud.com", ".qq.com")
UNAVAILABLE = 220030  # 「该文件获取失败，请至ima内查看处理」
QUOTA = 220021        # 「资料获取次数已达上限，请明天再尝试」
# 限制（实测，官方文档未写明）：连续约 50 次/分钟返回 200001「请求频率超限」（HTTP 403）；目录浏览 get_knowledge_list
# 与取原文 get_media_info 各有每日次数上限（各约几十到一百次），超出返回 QUOTA。检索不受每日上限限制，
# 所以本系统不按目录浏览，只检索标题，原文按每日预算下载
MIN_INTERVAL = 1.2  # 两次接口调用的最小间隔（秒），所有线程共用
_lock = threading.Lock()
_last = 0.0


class ImaError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(f"ima 接口错误 {code}：{msg}")
        self.code = code
        self.msg = msg


def credentials() -> tuple[str, str] | None:
    cid, key = os.environ.get(ENV_CLIENT_ID, "").strip(), os.environ.get(ENV_API_KEY, "").strip()
    return (cid, key) if cid and key else None


def call(session: requests.Session, path: str, body: dict) -> dict:
    creds = credentials()
    if not creds:
        raise ImaError(-1, f"未配置 {ENV_CLIENT_ID} / {ENV_API_KEY}")
    global _last
    with _lock:
        wait = _last + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last = time.monotonic()
    # 凭证只放在本次请求的头里，不写进 session（session 也用于访问其他网站）
    r = session.post(BASE + path, json=body, timeout=30,
                     headers={"ima-openapi-clientid": creds[0], "ima-openapi-apikey": creds[1]})
    try:
        data = r.json()  # 频控等业务错误也会带 HTTP 4xx，先看业务码
    except ValueError:
        r.raise_for_status()
        raise
    if data.get("code") != 0:
        raise ImaError(int(data.get("code") or -1), str(data.get("msg") or ""))
    return data.get("data") or {}


def knowledge_bases(session: requests.Session) -> list[dict]:
    """当前账号能访问的知识库（自建与已加入的共享库）：[{id, name, count, type, role}]。"""
    out, cursor = [], ""
    for _ in range(20):
        d = call(session, "search_knowledge_base", {"query": "", "cursor": cursor, "limit": 20})
        out += [{"id": x["kb_id"], "name": x.get("kb_name") or "", "count": int(x.get("content_count") or 0),
                 "type": x.get("base_type") or "", "role": x.get("role_type") or ""} for x in d.get("info_list") or []]
        cursor = d.get("next_cursor") or ""
        if d.get("is_end") or not cursor or not d.get("info_list"):
            break
    return out


def _entry(x: dict) -> dict:
    return {"media_id": x["media_id"], "title": (x.get("title") or "").strip(), "media_type": int(x.get("media_type") or 0),
            "parent": x.get("parent_folder_id") or ""}


def search(session: requests.Session, kb_id: str, query: str) -> list[dict]:
    """按标题搜索（服务端只匹配标题、多个词不取交集，最多约 100 条）。"""
    d = call(session, "search_knowledge", {"query": query, "knowledge_base_id": kb_id, "cursor": ""})
    return [_entry(x) for x in d.get("info_list") or []]


def media_url(session: requests.Session, media_id: str) -> tuple[str, dict] | None:
    """原文的临时下载地址与需要带的请求头；ima 不提供原文（笔记、录音、部分文件）时返回 None。"""
    try:
        d = call(session, "get_media_info", {"media_id": media_id})
    except ImaError as e:
        if e.code == UNAVAILABLE:
            return None
        raise
    info = d.get("url_info") or {}
    return (info["url"], dict(info.get("headers") or {})) if info.get("url") else None


def download(session: requests.Session, url: str, headers: dict, max_bytes: int) -> bytes:
    host = (urlparse(url).hostname or "").lower()
    if urlparse(url).scheme != "https" or not host.endswith(ALLOWED_HOSTS):
        raise ImaError(-1, f"原文地址不在 ima / 腾讯云域名下：{host}")
    with session.get(url, headers=headers, timeout=120, stream=True) as r:
        r.raise_for_status()
        buf = bytearray()
        for chunk in r.iter_content(1 << 16):
            buf += chunk
            if len(buf) > max_bytes:
                raise ImaError(-1, f"文件超过 {max_bytes // (1 << 20)} MB，未下载")
    return bytes(buf)


# ---------------------------------------------------------------- 标题解析

_DATE_PATTERNS = [
    re.compile(r"(?<!\d)(20\d{2})[-_.年/]?(\d{1,2})[-_.月/]?(\d{1,2})日?(?!\d)"),  # 20260929、2026-09-29、2026年9月29日
    re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)"),                           # 260310-GS-…、…-260309.pdf
    re.compile(r"(?<!\d)(\d{2})\.(\d{1,2})\.(\d{1,2})(?!\d)"),                     # 26.9.22
]
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_EN_DATE = [re.compile(r"(?<!\d)(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?,?\s+(20\d{2})"),   # 21 Jul 2026
            re.compile(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(20\d{2})")]         # Jul 21, 2026


def _mk(y: int, m: int, d: int, today: date) -> date | None:
    if y < 100:
        y += 2000
    try:
        dt = date(y, m, d)
    except ValueError:
        return None
    # 日期不应晚于今天，也不会早于知识库出现之前太久
    return dt if date(2015, 1, 1) <= dt <= today else None


def parse_date(text: str, today: date | None = None) -> date | None:
    """标题（或 OCR 文字）中的日期：取第一个合法日期。"""
    today = today or today_local()
    for p in _DATE_PATTERNS:
        for m in p.finditer(text):
            dt = _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)), today)
            if dt:
                return dt
    for i, p in enumerate(_EN_DATE):
        for m in p.finditer(text):
            if i == 0:
                d, mon, y = m.groups()
            else:
                mon, d, y = m.groups()
            if mon.lower()[:3] in _MONTHS:
                dt = _mk(int(y), _MONTHS[mon.lower()[:3]], int(d), today)
                if dt:
                    return dt
    return None


_BROKER = re.compile(r"^(?:深度个股-)?(?:\d{6,8})[-_](?P<who>[^-_（(：:]{2,12})[-_]")
_MEDIA = re.compile(r"^(?P<who>[^：:【】\s]{2,8})[：:]")
_BRACKET = re.compile(r"^【(?P<who>[^】]{2,12})】")


def source_name(title: str) -> str | None:
    """研报机构（「20260929-高盛-…」）、媒体（「彭博社：…」）或栏目（「【电报解读】…」）。"""
    for p in (_BROKER, _MEDIA, _BRACKET):
        m = p.match(title)
        if m:
            return m.group("who").strip()
    return None
