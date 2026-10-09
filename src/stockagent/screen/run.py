"""选股流程：L0 股票池与报表 → 事件扫描 → L1 硬过滤 → L2 四个桶打分 → 流动性核对 → L3 模型快筛 → 保存候选。

两个池分别保存一次运行（screen_runs），候选记下入选时的价格，供事后检验（stock screen eval）。
晋级到深度研究由使用者点选（stock screen promote / WebUI「送深研」）。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from ..app import FX_HKDCNH, App
from ..utils import log
from . import buckets, calendar, events, quick, store, themes, universe

POOL_BUCKETS = {"long": ("floor", "quality", "theme"), "event": ("event", "revision")}


@dataclass
class Result:
    msgs: list[str] = field(default_factory=list)
    runs: dict[str, int] = field(default_factory=dict)       # pool → run id


def fx_hkdcny(app: App) -> float | None:
    from ..tools import ensure_bars

    bars = ensure_bars(app, FX_HKDCNH)
    return float(bars["close"].iloc[-1]) if bars is not None and not bars.empty else None


def liquidity(app: App, c: dict) -> dict | None:
    """近 20 个交易日的成交额（上市地币种）：中位数（不受个别放量日影响）、均值、零成交天数（停牌）。
    日线没有成交额字段（腾讯备用源）时依次用换手率 × 市值、成交量 × 收盘价估算。取不到日线返回 None。"""
    from ..tools import ensure_bars

    bars = ensure_bars(app, c["secid"], max_age_days=4)
    if bars is None or len(bars) < 5:
        return None
    tail = bars.tail(20)
    amt, basis = tail["amount"], "成交额"
    if amt.dropna().shape[0] < 5:
        amt, basis = tail["turnover"] * c["metrics"]["mcap"], "换手率 × 市值估算"
    if amt.dropna().shape[0] < 5:  # 腾讯备用源的 A 股日线只有成交量（单位：手 = 100 股）
        lot = 100 if c["secid"].split(".")[0] in ("0", "1") else 1
        amt, basis = tail["volume"] * tail["close"] * lot, "成交量 × 收盘价估算"
    amt = amt.dropna()
    if len(amt) < 5:
        return None
    zero = int((tail["volume"].fillna(0) <= 0).sum())
    med = float(amt.median())
    return {"median": med, "mean": float(amt.mean()), "zero_days": zero, "days": len(tail), "basis": basis,
            "capacity": med * app.settings.screen.participation * 5}


def run(app: App, pools: tuple[str, ...] = ("long", "event"), markets: list[str] | None = None, use_llm: bool = True,
        refresh: bool = True, progress: Callable[[str], None] = print, hk_scope: str | None = None) -> Result:
    """hk_scope：all 全部港股 | connect 只看港股通标的；不填用配置 screen.hk_scope。"""
    cfg = app.settings.screen
    markets = markets or cfg.markets
    hk_scope = hk_scope or cfg.hk_scope
    res = Result()
    if refresh:
        res.msgs += universe.refresh(app, markets, progress=progress)
        res.msgs += events.scan(app, markets, progress=progress)
        res.msgs += calendar.refresh(app, markets, progress=progress)
    progress("代码打分…")
    mem, missing = themes.memberships(app)
    if missing:
        res.msgs.append("themes.yaml 中不在股票池的代码（不在股票池或代码有误）：" + "；".join(missing[:10])
                        + (f" 等 {len(missing)} 个" if len(missing) > 10 else ""))
    pool_rows = [u for m in markets for u in store.universe(app.db, m)]
    if not pool_rows:
        res.msgs.append("股票池为空：先运行 stock screen refresh（或去掉 --no-refresh）")
        return res
    rows = [u for u in pool_rows if universe.in_hk_scope(u, hk_scope)]
    if len(rows) < len(pool_rows):
        res.msgs.append(f"港股范围：仅港股通（排除非港股通港股 {len(pool_rows) - len(rows)} 只）")
    if not rows:
        res.msgs.append("港股范围内没有标的（先运行 stock screen refresh）")
        return res
    ctx = buckets.Ctx(cfg=cfg, hkdcny=fx_hkdcny(app) if "HK" in markets else None, events=events.recent(app), themes=mem,
                      calendar=calendar.build(app, rows))
    if "HK" in markets and ctx.hkdcny is None:
        res.msgs.append("✗ 没有港元兑人民币汇率：以人民币报账的港股无法计算 FCF 指标")
    dropped: Counter = Counter()
    evals = []
    for u in rows:
        why = buckets.hard_filter(u, cfg)
        if why:
            dropped[why] += 1
            continue
        evals.append(buckets.evaluate(u, ctx))
    res.msgs.append(f"股票池 {len(rows)} 只，硬过滤后 {len(evals)} 只" + (
        "（" + "，".join(f"{k} {v}" for k, v in dropped.most_common()) + "）" if dropped else ""))
    liq_cache: dict[str, dict | None] = {}
    for pool in pools:
        run_id = store.start_run(app.db, pool, {"markets": markets, "hk_scope": hk_scope, "use_llm": use_llm,
                                                 "cfg": cfg.model_dump()})
        try:
            stats = _pool(app, pool, evals, ctx, use_llm, liq_cache, progress)
        except Exception:
            store.finish_run(app.db, run_id, "failed", {})
            raise
        store.add_hits(app.db, run_id, stats.pop("hits"))
        store.finish_run(app.db, run_id, "done", stats)
        res.runs[pool] = run_id
        res.msgs.append(f"{buckets.POOL_LABEL[pool]}：候选 {stats['candidates']} 家（"
                        + "，".join(f"{t} 类 {stats['tiers'].get(t, 0)}" for t in "ABCD") + f"，快筛淘汰 {stats['tiers'].get('X', 0)}）"
                        + (f"，流动性不足剔除 {stats['illiquid']}" if stats["illiquid"] else ""))
    return res


def _pool(app: App, pool: str, evals: list[dict], ctx: buckets.Ctx, use_llm: bool, liq_cache: dict,
          progress: Callable[[str], None]) -> dict:
    cfg = app.settings.screen
    cands = [c for ev in evals if (c := buckets.pool_candidate(ev, pool, cfg))]
    by_bucket = {b: sum(1 for c in cands if b in c["buckets"].split(",")) for b in POOL_BUCKETS[pool]}
    chosen: dict[str, dict] = {}
    for b in POOL_BUCKETS[pool]:
        for c in buckets.top([c for c in cands if b in c["buckets"].split(",")], cfg.top_n):
            chosen[c["secid"]] = c
    picked = buckets.dedupe_ah(list(chosen.values()))
    progress(f"{buckets.POOL_LABEL[pool]}：{len(picked)} 家核对流动性…")
    kept, illiquid = [], 0
    for c in picked:
        if c["secid"] not in liq_cache:
            try:
                liq_cache[c["secid"]] = liquidity(app, c)
            except Exception as e:  # noqa: BLE001 - 行情源失败不影响其他候选
                log.info("取 %s 日线失败：%s", c["secid"], e)
                liq_cache[c["secid"]] = None
        liq = liq_cache[c["secid"]]
        c["metrics"]["liquidity"] = liq
        if liq is None:
            c["flags"].append({"code": "liquidity_unknown", "text": "没有取到日线，流动性未核对", "severity": "info"})
        elif liq["median"] < cfg.min_avg_amount.get(c["market"], 0):
            illiquid += 1
            continue
        elif liq["zero_days"] >= 3:
            c["flags"].append({"code": "suspended", "text": f"近 {liq['days']} 个交易日有 {liq['zero_days']} 天没有成交（停牌？）",
                               "severity": "high"})
            c["tier"] = "D" if c["tier"] in ("A", "B") else c["tier"]
        c["events"] = ctx.events.get(c["secid"], [])[:20]
        kept.append(c)
    kept.sort(key=lambda c: (buckets.TIER_ORDER.get(c["tier"], 9), -c["score"]))
    quick_n = 0
    if use_llm:
        quick_n = quick.run(app, [c for c in kept if c["tier"] in ("A", "B", "C")][: cfg.quick_top_n]
                            or kept[: cfg.quick_top_n], progress)
    tiers = Counter(c["tier"] for c in kept)
    return {"hits": kept, "candidates": len(kept), "illiquid": illiquid, "tiers": dict(tiers), "by_bucket": by_bucket,
            "evaluated": len(evals), "quick": quick_n}
