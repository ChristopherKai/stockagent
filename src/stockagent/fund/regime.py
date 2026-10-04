"""市场状态（每个市场每天一次，该市场所有基金共用）：进攻 / 中性 / 防守。

三个信号：指数收盘在 200 日线上方；宽度（日线股票池中站上 50 日线的比例，用选股器股票池近似，不用批量行情接口）；
20 日波动率在近一年中的分位。全部按缓存的日线计算，只读库。
- 进攻：三个信号都正常；防守：指数在均线下方且宽度 < 40%（宽度缺失时只看均线），或波动率分位 ≥ 95%；其余为中性。
"""

from __future__ import annotations

import math

import pandas as pd

from ..app import App
from .config import RegimeConfig

LABEL = {"attack": "进攻", "neutral": "中性", "defend": "防守"}


def vol_percentile(close: pd.Series, window: int, lookback: int) -> float | None:
    r = close.astype(float).pct_change()
    vol = r.rolling(window).std() * math.sqrt(252)
    hist = vol.dropna().tail(lookback)
    if len(hist) < max(window, 60):
        return None
    return float((hist <= hist.iloc[-1]).mean())


def breadth(bars: dict[str, pd.DataFrame], ma: int, as_of: str | None) -> tuple[float | None, int]:
    """站上 ma 日线的比例；只算最新一根是 as_of 那天的（停牌、数据没更新的不计）。"""
    up = n = 0
    for b in bars.values():
        if len(b) < ma or (as_of and str(b["date"].iloc[-1]) != as_of):
            continue
        c = b["close"].astype(float)
        n += 1
        up += float(c.iloc[-1]) > float(c.tail(ma).mean())
    return (up / n if n else None), n


def classify(above_ma: bool | None, breadth_v: float | None, vol_pct: float | None, cfg: RegimeConfig) -> tuple[str, list[str]]:
    weak = []
    if above_ma is False:
        weak.append(f"指数在 {cfg.ma} 日线下方")
    if breadth_v is not None and breadth_v < cfg.breadth_attack:
        weak.append(f"宽度 {breadth_v:.0%} 低于 {cfg.breadth_attack:.0%}")
    if vol_pct is not None and vol_pct >= cfg.vol_high:
        weak.append(f"波动率分位 {vol_pct:.0%}")
    if above_ma is None:
        return "neutral", ["指数日线不足，按中性处理"]
    if (above_ma is False and (breadth_v is None or breadth_v < cfg.breadth_defend)) or (vol_pct is not None and vol_pct >= cfg.vol_spike):
        return "defend", weak
    return ("neutral" if weak else "attack"), weak


def compute(app: App, cfg: RegimeConfig, market: str, universe_bars: dict[str, pd.DataFrame]) -> dict:
    sid = cfg.index[market]
    idx = app.bars(sid, limit=cfg.vol_lookback + cfg.ma + 40)
    out: dict = {"market": market, "index_secid": sid}
    above = None
    if idx is not None and len(idx) >= cfg.ma:
        c = idx["close"].astype(float)
        out.update(date=str(idx["date"].iloc[-1]), index_close=float(c.iloc[-1]), index_ma=float(c.tail(cfg.ma).mean()))
        above = out["index_close"] > out["index_ma"]
        out["vol_pct"] = vol_percentile(c, cfg.vol_window, cfg.vol_lookback)
    else:
        out.update(date=str(idx["date"].iloc[-1]) if idx is not None and len(idx) else None, vol_pct=None)
    b, n = breadth(universe_bars, cfg.breadth_ma, out.get("date"))
    out["breadth_n"] = n
    out["breadth"] = b if n >= cfg.min_breadth_n else None
    state, weak = classify(above, out["breadth"], out.get("vol_pct"), cfg)
    out.update(state=state, signals={"above_ma": above, "weak": weak, "breadth_sample": n, "ma": cfg.ma})
    return out


def describe(r: dict, cap: float) -> str:
    bits = []
    if r.get("index_close") and r.get("index_ma"):
        ma = (r.get("signals") or {}).get("ma", 200)
        bits.append(f"指数{'在' if r['index_close'] > r['index_ma'] else '跌破'} {ma} 日线")
    if r.get("breadth") is not None:
        bits.append(f"宽度 {r['breadth']:.0%}")
    if r.get("vol_pct") is not None:
        bits.append(f"波动率分位 {r['vol_pct']:.0%}")
    return f"{LABEL[r['state']]}（仓位上限 {cap:.0%}；" + "，".join(bits or ["信号不足"]) + "）"
