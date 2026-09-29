"""页面渲染用的过滤器：Markdown 净化、链接白名单、数字与时间格式。

深度报告、修正说明来自 LLM，而 LLM 读过不可信的网页：python-markdown 会原样放行 HTML 和 javascript: 链接，
所以渲染后一律经 nh3 按白名单净化，只保留排版标签与 http(s)/mailto 链接。
"""

from __future__ import annotations

import math
from urllib.parse import urlparse

import markdown as md
import nh3
from markupsafe import Markup

from ..utils import fmt_local

TAGS = {"a", "b", "blockquote", "br", "code", "del", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "li", "ol", "p",
        "pre", "s", "strong", "sub", "sup", "table", "tbody", "td", "th", "thead", "tr", "ul"}
ATTRS = {"a": {"href", "title"}, "td": {"align"}, "th": {"align"}}
SCHEMES = {"http", "https", "mailto"}


def md_html(text: str | None) -> Markup:
    if not text:
        return Markup("")
    html = md.markdown(text, extensions=["tables", "sane_lists"])
    return Markup(nh3.clean(html, tags=TAGS, attributes=ATTRS, url_schemes=SCHEMES, link_rel="noopener noreferrer"))


def safe_url(url: str | None) -> str:
    """只放行 http(s) 链接（事件原文、新闻来源）；其他协议（javascript: 等）返回空字符串。"""
    if not url:
        return ""
    try:
        return url if urlparse(url).scheme in ("http", "https") else ""
    except ValueError:
        return ""


def num(v, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "—"
    return f"{v:,.{digits}f}"


def pct(v, digits: int = 1, sign: bool = True) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "—"
    return f"{v * 100:+.{digits}f}%" if sign else f"{v * 100:.{digits}f}%"


def when(s: str | None, with_time: bool = True) -> str:
    return fmt_local(s, with_time=with_time) if s else "—"


def direction_class(v: float | None) -> str:
    """涨跌方向的样式类（颜色由页面的红涨绿跌 / 绿涨红跌设置决定）。"""
    if v is None or v == 0:
        return ""
    return "up" if v > 0 else "down"


def register(env) -> None:
    env.filters.update(md=md_html, safe_url=safe_url, num=num, pct=pct, when=when, dirclass=direction_class)
