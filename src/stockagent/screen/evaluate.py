"""选股器的事后检验：候选入选后 N 个交易日的收益与超额收益，按池 × 分层 × 策略桶统计。

起点是入选当日的价格记录（price_date 的收盘价，从日线里取），不含入选前的涨跌；
超额收益相对上市地的第一个基准指数（港股恒生指数，A 股沪深 300，见 watchlist.yaml benchmarks）。
「胜率」= 超额收益为正的比例。样本少时结论不可靠；同一标的在多次运行中重复入选的只取第一次。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from statistics import mean, median

from ..app import App
from ..utils import iso, utcnow
from . import store
from .buckets import BUCKET_LABEL, POOL_LABEL

HORIZONS = {"long": (20, 60, 120, 250), "event": (5, 20, 60)}
BENCH = {"HK": "100.HSI", "A": "1.000300"}


def _bench(app: App, market: str) -> str:
    q = app.watchlist.benchmarks.get(market)
    return q[0].secid if q else BENCH[market]


def _forward(bars, start: str, days: int) -> tuple[float, str, str] | None:
    dates = bars["date"].tolist()
    i = next((k for k, d in enumerate(dates) if d >= start), None)
    if i is None or i + days >= len(dates):
        return None
    return float(bars["close"].iloc[i + days]) / float(bars["close"].iloc[i]) - 1, dates[i], dates[i + days]


def collect(app: App, days: int = 400, fetch: bool = True) -> list[dict]:
    from ..tools import ensure_bars

    first: dict[tuple[str, str], dict] = {}
    for h in store.all_hits(app.db, iso(utcnow() - timedelta(days=days))):
        first.setdefault((h["pool"], h["secid"]), h)
    bars_cache: dict[str, object] = {}

    def bars(secid: str):
        if secid not in bars_cache:
            bars_cache[secid] = ensure_bars(app, secid, max_age_days=3) if fetch else app.bars(secid)
        return bars_cache[secid]

    rows = []
    for h in first.values():
        if not h.get("price_date"):
            continue
        b, bench = bars(h["secid"]), bars(_bench(app, h["market"]))
        row = {"pool": h["pool"], "tier": h["tier"], "buckets": h["buckets"].split(","), "name": h["name"], "code": h["code"]}
        for n in HORIZONS[h["pool"]]:
            r = _forward(b, h["price_date"], n) if b is not None else None
            if r is None:
                row[n] = None
                continue
            ret, d0, d1 = r
            bx = bench.set_index("date")["close"] if bench is not None else None
            if bx is not None and not bx[bx.index <= d0].empty:
                ret -= float(bx[bx.index <= d1].iloc[-1]) / float(bx[bx.index <= d0].iloc[-1]) - 1
            row[n] = ret
        rows.append(row)
    return rows


def summarize(rows: list[dict]) -> str:
    lines = []
    for pool, hs in HORIZONS.items():
        pr = [r for r in rows if r["pool"] == pool]
        if not pr:
            continue
        lines.append(f"\n{POOL_LABEL[pool]}：{len(pr)} 个候选（每只只计第一次入选）")
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in pr:
            groups[f"{r['tier']} 类"].append(r)
            for b in r["buckets"]:
                groups[f"桶·{BUCKET_LABEL.get(b, b)}"].append(r)
        head = "  分组".ljust(16) + "".join(f"{n} 日（n / 平均超额 / 中位 / 胜率）".rjust(34) for n in hs)
        lines.append(head)
        for g in sorted(groups):
            cells = []
            for n in hs:
                v = [r[n] for r in groups[g] if r.get(n) is not None]
                cells.append((f"{len(v)} / {mean(v):+.1%} / {median(v):+.1%} / {sum(x > 0 for x in v) / len(v):.0%}" if v else "—").rjust(34))
            lines.append(f"  {g}".ljust(16) + "".join(cells))
    return "\n".join(lines) if lines else "还没有候选记录（先运行 stock screen run）"
