"""最小的 5 段 cron 表达式：分 时 日 月 周。支持 *、*/n、a-b、a-b/n、逗号列表；周取 0–7（0 和 7 都是周日）。
日与周都有限定时按「或」匹配（与标准 cron 一致）。"""

from __future__ import annotations

from datetime import datetime, timedelta

_FIELDS = [("分钟", 0, 59), ("小时", 0, 23), ("日", 1, 31), ("月", 1, 12), ("星期", 0, 7)]


def _parse_field(expr: str, name: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in expr.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"{name}字段为空")
        rng, _, step_s = part.partition("/")
        step = 1
        if step_s:
            if not step_s.isdigit() or int(step_s) == 0:
                raise ValueError(f"{name}字段的步长不合法：{part}")
            step = int(step_s)
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a_s, _, b_s = rng.partition("-")
            if not (a_s.isdigit() and b_s.isdigit()):
                raise ValueError(f"{name}字段不合法：{part}")
            a, b = int(a_s), int(b_s)
        elif rng.isdigit():
            a = b = int(rng)
            if step_s:  # 「5/15」表示从 5 开始每 15
                b = hi
        else:
            raise ValueError(f"{name}字段不合法：{part}")
        if not (lo <= a <= hi and lo <= b <= hi and a <= b):
            raise ValueError(f"{name}字段超出范围 {lo}–{hi}：{part}")
        out.update(range(a, b + 1, step))
    return out


class Cron:
    def __init__(self, expr: str):
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError(f"cron 表达式需要 5 段（分 时 日 月 周），收到：{expr!r}")
        self.expr = expr
        sets = [_parse_field(p, name, lo, hi) for p, (name, lo, hi) in zip(parts, _FIELDS)]
        self.minutes, self.hours, self.days, self.months, dows = sets
        self.dows = {0 if d == 7 else d for d in dows}
        self.dom_any, self.dow_any = parts[2] == "*", parts[4] == "*"

    def _day_ok(self, dt: datetime) -> bool:
        dom = dt.day in self.days
        dow = (dt.weekday() + 1) % 7 in self.dows  # Python：周一=0；cron：周日=0
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow
        if self.dow_any:
            return dom
        return dom or dow

    def match(self, dt: datetime) -> bool:
        return (dt.minute in self.minutes and dt.hour in self.hours and dt.month in self.months
                and self._day_ok(dt))

    def next_after(self, dt: datetime, max_days: int = 400) -> datetime | None:
        """dt 之后的下一次触发时间（精确到分钟）；按天、按小时跳过不可能的时段。"""
        t = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        end = t + timedelta(days=max_days)
        while t < end:
            if t.month not in self.months or not self._day_ok(t):
                t = (t + timedelta(days=1)).replace(hour=0, minute=0)
            elif t.hour not in self.hours:
                t = (t + timedelta(hours=1)).replace(minute=0)
            elif t.minute in self.minutes:
                return t
            else:
                t += timedelta(minutes=1)
        return None


def validate(expr: str) -> str:
    Cron(expr)
    return expr
