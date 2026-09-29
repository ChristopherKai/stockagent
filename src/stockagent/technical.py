"""技术面快照、事件后的价格反应、AH 溢价。全部由代码计算，不经过 LLM。"""

from __future__ import annotations

import math
from datetime import datetime

import pandas as pd

from .utils import CST, NY

# 收盘时间（本地时区）。港股收市竞价约 16:10 结束
CLOSE_TIME = {"HK": (16, 10), "A": (15, 0), "US": (16, 0)}


def _ret(close: pd.Series, k: int) -> float | None:
    return float(close.iloc[-1] / close.iloc[-1 - k] - 1) if len(close) > k else None


def trend_label(close: float, ma20: float | None, ma60: float | None, slope: float | None) -> str:
    if ma20 is None:
        return "数据不足"
    if ma60 is None:
        return "站上20日线" if close > ma20 else "跌破20日线"
    if close > ma20 > ma60 and (slope or 0) > 0:
        return "上升趋势"
    if close < ma20 < ma60 and (slope or 0) < 0:
        return "下降趋势"
    if ma20 > ma60 and close <= ma20:
        return "上升趋势中回调"
    if ma20 < ma60 and close >= ma20:
        return "下降趋势中反弹"
    return "震荡"


def snapshot(bars: pd.DataFrame, bench: pd.DataFrame | None = None) -> dict | None:
    if bars is None or len(bars) < 2:
        return None
    c, h, l, v = bars["close"], bars["high"], bars["low"], bars["volume"]
    n = len(bars)
    last = float(c.iloc[-1])

    def ma(k: int) -> float | None:
        return float(c.tail(k).mean()) if n >= k else None

    ma20_series = c.rolling(20).mean()
    slope = float(ma20_series.iloc[-1] / ma20_series.iloc[-6] - 1) if n >= 25 else None

    rsi = None
    if n > 15:
        delta = c.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
        rsi = 100.0 if loss == 0 else float(100 - 100 / (1 + gain / loss))

    atr_pct = None
    if n > 15:
        prev = c.shift(1)
        tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
        atr_pct = float(tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1] / last)

    vol20 = float(c.pct_change().tail(20).std() * math.sqrt(252)) if n > 21 else None
    avg_vol = float(v.iloc[-21:-1].mean()) if n > 21 else None
    window = bars.tail(250)
    hi, lo = float(window["high"].max()), float(window["low"].min())
    ma5, ma20, ma60 = ma(5), ma(20), ma(60)

    out = {
        "date": bars["date"].iloc[-1],
        "close": last,
        "ret_1d": _ret(c, 1), "ret_5d": _ret(c, 5), "ret_20d": _ret(c, 20), "ret_60d": _ret(c, 60),
        "ma5": ma5, "ma20": ma20, "ma60": ma60, "ma20_slope": slope,
        "rsi14": rsi, "atr_pct": atr_pct, "vol20": vol20,
        "vol_ratio": float(v.iloc[-1] / avg_vol) if avg_vol else None,
        "high_250": hi, "low_250": lo, "from_high": last / hi - 1, "from_low": last / lo - 1,
        "bars": n,
        "trend": trend_label(last, ma20, ma60, slope),
    }
    if bench is not None and len(bench) > 1:
        m = bars[["date", "close"]].merge(bench[["date", "close"]], on="date", suffixes=("", "_b"))
        for k in (5, 20):
            s, b = _ret(m["close"], k), _ret(m["close_b"], k)
            out[f"excess_{k}d"] = s - b if s is not None and b is not None else None
    return out


def reaction(bars: pd.DataFrame, published_at: datetime, market: str,
             bench: pd.DataFrame | None = None) -> dict | None:
    """消息发布后的价格反应：以发布前最后一个收盘价为基准，到最新收盘价。"""
    if bars is None or bars.empty:
        return None
    t = published_at.astimezone(NY if market == "US" else CST)
    day = t.strftime("%Y-%m-%d")
    before_close = (t.hour, t.minute) < CLOSE_TIME[market]
    dates = bars["date"].tolist()
    base_idx = None
    for i, d in enumerate(dates):
        if d < day or (d == day and not before_close):
            base_idx = i
        else:
            break
    if base_idx is None:
        return None
    days = len(dates) - 1 - base_idx
    if days == 0:
        return {"days": 0, "ret": None, "excess": None}
    base = float(bars["close"].iloc[base_idx])
    ret = float(bars["close"].iloc[-1]) / base - 1
    excess = None
    if bench is not None and not bench.empty:
        b = bench.set_index("date")["close"]
        base_date, last_date = dates[base_idx], dates[-1]
        b_base = b[b.index <= base_date]
        b_last = b[b.index <= last_date]
        if not b_base.empty and not b_last.empty:
            excess = ret - (float(b_last.iloc[-1]) / float(b_base.iloc[-1]) - 1)
    return {"days": days, "ret": ret, "excess": excess}


def forward_return(bars: pd.DataFrame, published_at: datetime, market: str, days: int,
                   bench: pd.DataFrame | None = None, tradable: bool = True) -> dict | None:
    """消息发布后 days 个交易日的收益；数据不足返回 None。
    tradable=True（可交易口径）：从发布后第一个能成交的收盘价起算——盘中发布用当日收盘，收盘后发布用下一交易日收盘，
    不把消息所描述的当日波动算进去；tradable=False：与 reaction 相同，从发布前最后一个收盘价起算。"""
    if bars is None or bars.empty:
        return None
    t = published_at.astimezone(NY if market == "US" else CST)
    day = t.strftime("%Y-%m-%d")
    before_close = (t.hour, t.minute) < CLOSE_TIME[market]
    dates = bars["date"].tolist()
    if tradable:
        base_idx = next((i for i, d in enumerate(dates) if d > day or (d == day and before_close)), None)
    else:
        base_idx = None
        for i, d in enumerate(dates):
            if d < day or (d == day and not before_close):
                base_idx = i
            else:
                break
    if base_idx is None or base_idx + days >= len(dates):
        return None
    base, end = float(bars["close"].iloc[base_idx]), float(bars["close"].iloc[base_idx + days])
    ret = end / base - 1
    excess = None
    if bench is not None and not bench.empty:
        b = bench.set_index("date")["close"]
        b0, b1 = b[b.index <= dates[base_idx]], b[b.index <= dates[base_idx + days]]
        if not b0.empty and not b1.empty:
            excess = ret - (float(b1.iloc[-1]) / float(b0.iloc[-1]) - 1)
    return {"ret": ret, "excess": excess}


def ah_premium(a_bars: pd.DataFrame, h_bars: pd.DataFrame, fx_bars: pd.DataFrame) -> dict | None:
    """A 股相对 H 股的溢价：A价 / (H价 × 港币兑人民币) − 1。fx_bars 为 HKDCNH 日线。"""
    if any(x is None or x.empty for x in (a_bars, h_bars, fx_bars)):
        return None
    m = (a_bars[["date", "close"]].rename(columns={"close": "a"})
         .merge(h_bars[["date", "close"]].rename(columns={"close": "h"}), on="date"))
    if m.empty:
        return None
    # 汇率可能晚一天发布或遇假日缺失：取当日或之前最近的汇率
    fx = fx_bars[["date", "close"]].rename(columns={"close": "fx"}).copy()
    m["_d"], fx["_d"] = pd.to_datetime(m["date"]), pd.to_datetime(fx["date"])
    m = pd.merge_asof(m.sort_values("_d"), fx.drop(columns="date").sort_values("_d"), on="_d", direction="backward")
    m = m.dropna(subset=["fx"])
    if m.empty:
        return None
    prem = m["a"] / (m["h"] * m["fx"]) - 1
    tail = prem.tail(60)
    return {
        "date": m["date"].iloc[-1],
        "current": float(prem.iloc[-1]),
        "avg_20": float(prem.tail(20).mean()),
        "min_60": float(tail.min()),
        "max_60": float(tail.max()),
        "samples": len(prem),
    }
