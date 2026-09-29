"""事后检验：已分级事件发布后的超额收益，检验「方向」与「重要性」判断有没有预测力。

只统计相关、非重复、方向为利好或利空的事件；同一事件多条报道只算一次（依赖分级时的去重）。
这是前向检验：只有事件发布之后的行情才计入。收益按可交易口径计算——从消息发布后第一个能成交的收盘价起算，
不把新闻所描述的当日涨跌（例如「某股大跌 9%」这类报道）算成预测成功。
"""

from __future__ import annotations

from datetime import timedelta

from . import technical
from .app import App
from .config import Company
from .triage import listing_market
from .utils import iso, parse_iso, pct, utcnow

HORIZONS = (1, 5, 20)


def collect(app: App, companies: list[Company], days: int = 90) -> list[dict]:
    since = iso(utcnow() - timedelta(days=days))
    rows = []
    for c in companies:
        for e in app.db.items_since(c.id, since, min_materiality=2):
            if e["direction"] not in ("positive", "negative"):
                continue
            listing = c.listing(listing_market(app, c, e)) or c.listings[0]
            bars, bench = app.bars(app.secid(listing)), app.bars(app.bench_secid(listing))
            row = {"company": c.name, "materiality": e["materiality"], "direction": e["direction"]}
            for h in HORIZONS:
                r = technical.forward_return(bars, parse_iso(e["published_at"]), listing.market, h, bench)
                row[h] = None if r is None else (r["excess"] if r["excess"] is not None else r["ret"])
            rows.append(row)
    return rows


def summarize(rows: list[dict]) -> str:
    """按（重要性分组 × 方向）统计：样本数、平均超额收益、方向命中率（超额收益符号与判断一致的比例）。"""
    groups = {("4–5", "positive"): [], ("4–5", "negative"): [], ("2–3", "positive"): [], ("2–3", "negative"): []}
    for r in rows:
        groups[("4–5" if r["materiality"] >= 4 else "2–3", r["direction"])].append(r)
    head = "| 重要性 | 方向 | " + " | ".join(f"{h}日样本 | {h}日平均超额 | {h}日命中率" for h in HORIZONS) + " |"
    out = [head, "|---|---|" + "---|---|---|" * len(HORIZONS)]
    for (bucket, direction), rs in groups.items():
        cells = []
        for h in HORIZONS:
            vals = [r[h] for r in rs if r[h] is not None]
            if not vals:
                cells += ["0", "—", "—"]
                continue
            sign = 1 if direction == "positive" else -1
            hit = sum(1 for v in vals if v * sign > 0) / len(vals)
            cells += [str(len(vals)), pct(sum(vals) / len(vals)), pct(hit, 0, sign=False)]
        out.append(f"| {bucket} | {'利好' if direction == 'positive' else '利空'} | " + " | ".join(cells) + " |")
    return "\n".join(out)
