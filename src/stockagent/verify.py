"""数字溯源：报告里的数字能否在研究笔记或工具返回结果中找到对应（允许四舍五入、亿/万单位换算）。

这是启发式检查——找得到不代表一定用对了地方，找不到也可能只是写法不同；它的用途是把
「没有出处的数字」挑出来，提醒人工核对，而不是给报告打分。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NUM_RE = re.compile(r"(?<![\w.])([-+]?\d[\d,]*(?:\.\d+)?)\s*(%|亿|万)?")
_SKIP_CONTEXT = re.compile(r"第\s*$|p\s*$|P\s*$|§\s*$")


@dataclass
class Check:
    total: int
    verified: int
    unverified: list[str]

    @property
    def ratio(self) -> float:
        return self.verified / self.total if self.total else 1.0


def _numbers(text: str) -> list[tuple[str, float, int]]:
    """返回 [(原文写法, 数值（亿/万已换算成个位）, 小数位数)]。"""
    out = []
    for m in _NUM_RE.finditer(text):
        raw, unit = m.group(1), m.group(2) or ""
        if _SKIP_CONTEXT.search(text[max(0, m.start() - 2) : m.start()]):
            continue  # 页码、章节号
        s = raw.replace(",", "")
        try:
            v = float(s)
        except ValueError:
            continue
        decimals = len(s.split(".")[1]) if "." in s else 0
        scale = {"亿": 1e8, "万": 1e4}.get(unit, 1.0)
        out.append((raw + unit, v * scale, decimals))
    return out


def _interesting(raw: str, value: float, decimals: int) -> bool:
    """跳过年份、日期片段、小整数等不需要溯源的数字。"""
    plain = raw.rstrip("%亿万").replace(",", "")
    if decimals == 0 and not raw.endswith(("%", "亿", "万")):
        v = abs(value)
        if v < 100 or (1900 <= v <= 2100):
            return False
    return len(plain.replace("-", "").replace("+", "").replace(".", "")) >= 2


def check(report: str, sources: list[str]) -> Check:
    corpus = [(v, d) for text in sources for _, v, d in _numbers(text)]
    values = [v for v, _ in corpus]
    seen: dict[str, bool] = {}
    for raw, value, decimals in _numbers(report):
        if raw in seen or not _interesting(raw, value, decimals):
            continue
        unit_scaled = raw.endswith(("亿", "万"))
        ok = False
        for src in values:
            if unit_scaled:  # 单位换算后的大数：相对误差 0.5% 内视为同一数字；财报常以千元、万元列示
                if any(src and abs(src * k - value) <= abs(value) * 0.005 for k in (1, 1e3, 1e4)):
                    ok = True
                    break
            elif round(src, decimals) == round(value, decimals) or round(abs(src), decimals) == round(abs(value), decimals):
                ok = True
                break
            elif raw.endswith("%") and round(src * 100, decimals) == round(value, decimals):  # 0.3702 ↔ 37.02%
                ok = True
                break
        seen[raw] = ok
    unverified = [k for k, ok in seen.items() if not ok]
    return Check(total=len(seen), verified=len(seen) - len(unverified), unverified=unverified)


def markdown(result: Check, limit: int = 60) -> str:
    head = (f"报告中共 {result.total} 个需要溯源的数字，{result.verified} 个能在研究笔记或工具结果中找到对应"
            f"（{result.ratio:.0%}）。")
    if not result.unverified:
        return head
    shown = "、".join(result.unverified[:limit]) + ("……" if len(result.unverified) > limit else "")
    return head + f"以下 {len(result.unverified)} 个未找到对应（可能是推算、改写或错误，请人工核对）：{shown}"
