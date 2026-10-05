"""HTML → 纯文本（带页码标记与表格行）：SEC 申报文件、新闻稿正文等共用。"""

from __future__ import annotations

import re

_PAGE_MARK = "\x00PAGE\x00"
_BREAK_BEFORE = re.compile(r"page-break-before\s*:\s*always|break-before\s*:\s*page", re.I)
_BREAK_AFTER = re.compile(r"page-break-after\s*:\s*always|break-after\s*:\s*page", re.I)
_HIDDEN = re.compile(r"display\s*:\s*none", re.I)


def html_text(html: str) -> tuple[str, int | None]:
    """HTML → (文本, 页数)。表格每行一行（单元格用 | 隔开，「$」「)」「%」并入相邻数字）；
    SEC 申报文件的 iXBRL 隐藏头（ix:header）与 display:none 的块去掉；文件按「分页」样式切页并插入 [第N页]，没有分页样式则不分页。"""
    from bs4 import BeautifulSoup, NavigableString

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "ix:header"]):
        tag.decompose()
    for tag in soup.find_all(style=_HIDDEN):
        tag.decompose()
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c and c != "$"]
        merged: list[str] = []
        for c in cells:
            if merged and c in (")", "%", ")%"):
                merged[-1] += c
            elif merged and merged[-1] == "(":
                merged[-1] = "(" + c
            else:
                merged.append(c)
        tr.replace_with(NavigableString("\n" + " | ".join(merged) + "\n"))
    for tag in soup.find_all(style=_BREAK_BEFORE):
        tag.insert_before(NavigableString(_PAGE_MARK))
    for tag in soup.find_all(style=_BREAK_AFTER):
        tag.insert_after(NavigableString(_PAGE_MARK))
    text = soup.get_text("\n", strip=True)
    if _PAGE_MARK not in text:
        return text, None
    pages = [t for t in (re.sub(r"\n{2,}", "\n", p).strip() for p in text.split(_PAGE_MARK)) if t]
    return "\n\n".join(f"[第{i}页]\n{t}" for i, t in enumerate(pages, 1)), len(pages)
