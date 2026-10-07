"""各市场的交易时段（本地时间）：A 股、港股用 fund/calendar.py 的休市日历与半日市，美股只判周一至周五（没有休市日历）。

- A 股 9:30–11:30、13:00–15:00（北京时间）；港股 9:30–12:00、13:00–16:00，半日市只到 12:00；
  美股 9:30–16:00（America/New_York，夏令时自动切换）。
- 时段按左闭右开处理：15:00 整已经收盘。
- 现价展示（valuation.display_price）与行情刷新（quotes.py）共用；纯函数，测试里传 now 即可。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .fund import calendar
from .utils import CST, NY, utcnow

TZ: dict[str, ZoneInfo] = {"A": CST, "HK": CST, "US": NY}
_SESSIONS: dict[str, tuple[tuple[time, time], ...]] = {
    "A": ((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0))),
    "HK": ((time(9, 30), time(12, 0)), (time(13, 0), time(16, 0))),
    "US": ((time(9, 30), time(16, 0)),),
}
REFRESH_GRACE_MIN = 10  # 收盘后多少分钟内仍刷新一次，拿到收盘价（港股收盘竞价到 16:10 前后才出最终价）


def now() -> datetime:
    """当前时刻（带时区）。测试里替换它来模拟时间。"""
    return utcnow()


def local_dt(market: str, ts: datetime) -> datetime:
    return ts.astimezone(TZ[market])


def local_day(market: str, ts: datetime) -> date:
    """ts 在该市场当地的日期（美股按美东日期，与日线的交易日一致）。"""
    return local_dt(market, ts).date()


def is_trading_day(market: str, d: date) -> bool:
    if market == "US":
        return d.weekday() < 5
    return calendar.is_trading_day(market, d)


def sessions(market: str, d: date) -> list[tuple[datetime, datetime]]:
    """d 这一天该市场的交易时段 [(开, 收)]（当地时间，带时区）；休市日为空，港股半日市只有上午盘。"""
    if not is_trading_day(market, d):
        return []
    spans = _SESSIONS[market]
    if market == "HK" and calendar.is_half_day("HK", d):
        spans = spans[:1]
    tz = TZ[market]
    return [(datetime.combine(d, a, tz), datetime.combine(d, b, tz)) for a, b in spans]


def is_open(market: str, ts: datetime | None = None) -> bool:
    ts = local_dt(market, ts or now())
    return any(a <= ts < b for a, b in sessions(market, ts.date()))


def day_close(market: str, d: date) -> datetime | None:
    """d 这一天最后一个时段的收盘时刻；休市日为 None。"""
    s = sessions(market, d)
    return s[-1][1] if s else None


def in_refresh_window(market: str, ts: datetime | None = None, grace_min: int = REFRESH_GRACE_MIN) -> bool:
    """开盘中，或刚收盘不久（用来取收盘价）。"""
    ts = local_dt(market, ts or now())
    if is_open(market, ts):
        return True
    close = day_close(market, ts.date())
    return close is not None and close <= ts < close + timedelta(minutes=grace_min + 1)


def is_final(market: str, ts: datetime) -> bool:
    """ts 时刻取得的行情是否已是当日收盘后的（休市日取到的也是上一交易日收盘价）。"""
    t = local_dt(market, ts)
    close = day_close(market, t.date())
    return close is None or t >= close
