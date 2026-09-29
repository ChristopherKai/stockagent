"""通用工具：时间、HTTP 会话、文本清洗。"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from zhconv import convert

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("stockagent")

# 港股与 A 股都是 UTC+8，无夏令时
CST = ZoneInfo("Asia/Shanghai")
NY = ZoneInfo("America/New_York")

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """统一以 UTC ISO 字符串入库。"""
    if dt.tzinfo is None:
        raise ValueError("naive datetime 不允许入库")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def local(dt: datetime, tz: ZoneInfo = CST) -> datetime:
    return dt.astimezone(tz)


def fmt_local(s: str | None, tz: ZoneInfo = CST, with_time: bool = True) -> str:
    if not s:
        return ""
    dt = local(parse_iso(s), tz)
    return dt.strftime("%m-%d %H:%M" if with_time else "%Y-%m-%d")


def today_local(tz: ZoneInfo = CST) -> date:
    return datetime.now(tz).date()


def days_ago(n: int) -> datetime:
    return utcnow() - timedelta(days=n)


def sha1(*parts: object) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(s: str | None) -> str:
    """去 HTML 标签与实体、压缩空白。"""
    if not s:
        return ""
    s = html.unescape(_TAG_RE.sub(" ", s))
    return _WS_RE.sub(" ", s).strip()


def normalize_title(s: str) -> str:
    """用于跨来源去重的标题归一化：去标点空白、统一大小写。"""
    s = clean_text(s).lower()
    return re.sub(r"[\s\W_]+", "", s)


def jsonp_body(text: str) -> dict:
    start, end = text.find("("), text.rfind(")")
    if start == -1 or end <= start:
        raise ValueError("不是 JSONP 响应")
    return json.loads(text[start + 1 : end])


def to_float(v) -> float | None:
    if v is None or v == "" or v == "-":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class ThrottledSession(requests.Session):
    """按域名限制请求间隔。东方财富等站点短时间请求过密会直接断开连接。"""

    def __init__(self, min_interval: float = 0.6):
        super().__init__()
        self.min_interval = min_interval
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def request(self, method, url, *args, **kwargs):
        host = urlparse(url).netloc
        with self._lock:
            wait = self._last.get(host, 0) + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()
        return super().request(method, url, *args, **kwargs)


def make_session(min_interval: float = 0.6, user_agent: str = BROWSER_UA) -> requests.Session:
    s = ThrottledSession(min_interval)
    retry = Retry(
        total=3,
        backoff_factor=2.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers["User-Agent"] = user_agent
    return s


def pct(v: float | None, digits: int = 1, sign: bool = True) -> str:
    if v is None:
        return "—"
    return f"{v * 100:+.{digits}f}%" if sign else f"{v * 100:.{digits}f}%"


def num(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:,.{digits}f}"


def big_num(v: float | None) -> str:
    """市值等大数：亿 / 万亿。"""
    if v is None:
        return "—"
    if abs(v) >= 1e12:
        return f"{v / 1e12:.2f}万亿"
    if abs(v) >= 1e8:
        return f"{v / 1e8:.1f}亿"
    return f"{v:,.0f}"


def to_simplified(text: str) -> str:
    """检索时把原文和关键词统一转成简体再比较。不能反过来把关键词转成繁体：
    「东岳」会被转成「東嶽」，而港股年报原文写作「東岳」，两种写法都匹配不上。"""
    return convert(text, "zh-hans")
