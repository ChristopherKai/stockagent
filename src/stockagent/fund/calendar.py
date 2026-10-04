"""港股与 A 股交易日历（休市日，不含周末）。

来源（2026-10 核对）：
- 港股：香港特区政府公布的公众假期（https://www.gov.hk/en/about/abouthk/holiday/2026.htm、/2027.htm），落在工作日的即休市；
  圣诞、元旦、农历新年前夕只有上午盘（HALF_DAYS）。恶劣天气不再停市（2024-09 起），不计。
- A 股：上交所《关于上海证券交易所2026年部分节假日休市安排的通知》（上证公告〔2025〕45号）；深交所、北交所同步。
- A 股 2027 年的安排要等国务院办公厅年底公布节假日后交易所才发布，目前未核对：VERIFIED 不含该年，
  按周一至周五处理并在计划里提示（unverified）。公布后把日期补进 HOLIDAYS、年份加进 VERIFIED。
"""

from __future__ import annotations

from datetime import date, timedelta

HOLIDAYS: dict[str, dict[int, tuple[str, ...]]] = {
    "HK": {
        2026: ("2026-01-01", "2026-02-17", "2026-02-18", "2026-02-19", "2026-04-03", "2026-04-06", "2026-04-07",
               "2026-05-01", "2026-05-25", "2026-06-19", "2026-07-01", "2026-10-01", "2026-10-19", "2026-12-25"),
        2027: ("2027-01-01", "2027-02-08", "2027-02-09", "2027-03-26", "2027-03-29", "2027-04-05", "2027-05-13",
               "2027-06-09", "2027-07-01", "2027-09-16", "2027-10-01", "2027-10-08", "2027-12-27"),
    },
    "A": {
        2026: ("2026-01-01", "2026-01-02", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
               "2026-02-23", "2026-04-06", "2026-05-01", "2026-05-04", "2026-05-05", "2026-06-19", "2026-09-25",
               "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"),
    },
}
HALF_DAYS: dict[str, tuple[str, ...]] = {
    "HK": ("2026-02-16", "2026-12-24", "2026-12-31", "2027-02-05", "2027-12-24", "2027-12-31"),
}
VERIFIED: dict[str, set[int]] = {"HK": {2026, 2027}, "A": {2026}}

_SETS = {m: {date.fromisoformat(d) for ds in years.values() for d in ds} for m, years in HOLIDAYS.items()}


def verified(market: str, d: date) -> bool:
    return d.year in VERIFIED.get(market, set())


def is_trading_day(market: str, d: date) -> bool:
    return d.weekday() < 5 and d not in _SETS.get(market, set())


def is_half_day(market: str, d: date) -> bool:
    return d.isoformat() in HALF_DAYS.get(market, ())


def next_trading_day(market: str, d: date) -> date:
    """d 之后（不含 d）的第一个交易日。"""
    d += timedelta(days=1)
    while not is_trading_day(market, d):
        d += timedelta(days=1)
    return d


def prev_trading_day(market: str, d: date) -> date:
    """d 之前（不含 d）的最近一个交易日。"""
    d -= timedelta(days=1)
    while not is_trading_day(market, d):
        d -= timedelta(days=1)
    return d


def trading_days_between(market: str, start: date, end: date) -> int:
    """(start, end] 之间的交易日数。"""
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        n += is_trading_day(market, d)
    return n


def first_of_period(market: str, d: date, period: str) -> bool:
    """d 是否为本周 / 本月 / 本季度的第一个交易日（调仓日）。"""
    if not is_trading_day(market, d):
        return False
    p = prev_trading_day(market, d)
    if period == "weekly":
        return p.isocalendar()[:2] != d.isocalendar()[:2]
    if period == "monthly":
        return (p.year, p.month) != (d.year, d.month)
    if period == "quarterly":
        return (p.year, (p.month - 1) // 3) != (d.year, (d.month - 1) // 3)
    raise ValueError(f"未知调仓周期：{period}")


def note(market: str, d: date) -> str | None:
    """日历未核对时给计划加的提示。"""
    if verified(market, d):
        return None
    label = {"HK": "港股", "A": "A 股"}[market]
    return f"{label} {d.year} 年休市安排尚未录入（交易所通常在上一年年底公布），暂按周一至周五处理"
