"""策略插件：只回答「选什么、目标权重多少、止损在哪、为什么」，看不到现金与订单。

接口：
- score(ctx) -> list[Target]：候选及目标权重（风控前的满仓权重）、止损、入场条件、理由；
- check_exit(position, ctx) -> ExitSignal | None：止损、论点失效、止盈、复核；
- add_signal(position, ctx) -> Target | None：分批建仓的下一批（价值类回调或调仓日补齐；趋势类只在盈利时加）；
- update(position, ctx) -> dict：持仓的止损、最高收盘价等随行情更新的字段。
仓位上限、熔断、取整、先卖后买都在 risk.py。全部由代码计算，不调用模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import ClassVar

import pandas as pd
from pydantic import BaseModel, ConfigDict

from ..app import App
from . import calendar


@dataclass
class Target:
    secid: str
    code: str
    name: str
    market: str
    score: float
    weight: float                    # 计划的满仓权重（风控前）
    price: float                     # 参考价（最新收盘）
    stop: float | None = None
    entry: dict = field(default_factory=lambda: {"type": "open"})   # open 开盘买 | breakout 突破条件单
    tranches: int = 1                # 计划分几批建仓
    reason: str = ""
    sector: str | None = None
    org_code: str | None = None
    metrics: dict = field(default_factory=dict)


@dataclass
class ExitSignal:
    action: str                      # sell 清仓 | trim 减仓 | review 复核提醒（不交易）
    fraction: float                  # 卖出持仓的比例
    priority: int                    # 1 止损 2 论点失效 3 风控降仓 4 止盈 5 替换 / 调仓
    reason: str
    kind: str = "open"               # open 开盘卖 | note 只提醒


@dataclass
class Context:
    """策略可见的数据（不含现金、订单）。由 engine 按市场准备一次，多只基金共用。"""

    app: App
    market: str
    as_of: date                      # 计划日期
    expected: date                   # 计划依据的收盘日
    universe: list[dict]             # 第一阶段后的股票（screen_universe 行）
    ind: dict[str, dict]             # secid → 指标（data.indicators）
    min_turnover: float
    participation: float
    rebalance_day: bool = False
    trackers: dict[str, dict] = field(default_factory=dict)   # secid → {target, ratio, company_id, company}
    kill: dict[str, list[dict]] = field(default_factory=dict)  # secid → 证伪条件（已触发 / 苗头，含 falsifies）
    hkdcny: float | None = None
    notes: list[str] = field(default_factory=list)

    def row(self, secid: str) -> dict | None:
        return next((u for u in self.universe if u["secid"] == secid), None)


class Strategy:
    key: ClassVar[str]
    label: ClassVar[str]
    market: ClassVar[str]
    style: ClassVar[str]

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

    def __init__(self, params: dict | None = None):
        self.p = self.Params.model_validate(params or {})

    def score(self, ctx: Context) -> list[Target]:
        raise NotImplementedError

    def check_exit(self, pos: dict, ctx: Context) -> ExitSignal | None:
        raise NotImplementedError

    def add_signal(self, pos: dict, ctx: Context) -> Target | None:
        return None

    def update(self, pos: dict, ctx: Context) -> dict:
        ind = ctx.ind.get(pos["secid"])
        if not ind:
            return {}
        return {"high_close": max(pos.get("high_close") or 0.0, ind["close"])}


def _rank_pct(values: pd.Series, ascending: bool = False) -> pd.Series:
    """百分位排名：0 = 最好。"""
    return values.rank(ascending=ascending, method="average", pct=True) - 1 / max(len(values), 1) / 2


def kill_exit(pos: dict, ctx: Context) -> ExitSignal | None:
    """持仓公司的证伪条件已触发（与立场相反的方向）：exit 清仓、reduce 减半；苗头只提醒。"""
    for t in ctx.kill.get(pos["secid"], []):
        if t["status"] == "triggered" and t["falsifies"] and t["action"] in ("exit", "reduce"):
            frac = 1.0 if t["action"] == "exit" else 0.5
            return ExitSignal("sell" if frac == 1 else "trim", frac, 2, f"证伪条件触发：{t['description']}")
    return None


def _triggered(ctx: Context, secid: str) -> str | None:
    for t in ctx.kill.get(secid, []):
        if t["status"] == "triggered" and t["falsifies"]:
            return t["description"]
    return None


# ---------------------------------------------------------------- 港股质量价值（巴菲特、格林布拉特）


class HKQualityValue(Strategy):
    """港股通全市场按「质量 + 便宜」综合排名（格林布拉特式）；有情景加权目标价的再加安全边际门槛。

    质量：ROE（年化）≥ min_roe、资产负债率 ≤ max_debt_ratio（或净现金）、近 3 个年度 FCF 为正的年数、毛利率稳定；
    便宜：盈利收益率（1 / PE TTM）与保守 FCF Yield 的排名平均（股息率作并列时的参考）。
    买：有目标价的现价 ≤ 目标价 × mos；没有的须便宜程度排在前 cheap_quantile。分 3 批，按综合得分在 min_weight–max_weight 间定权重。
    卖：证伪条件触发；现价 ≥ 目标价（没有目标价时用选股器口径的合理价值）× take_profit；调仓日质量条件不再满足。
    不设机械止损：较成本跌 review_drop 时推送复核提醒。银行、保险等 FCF 口径不适用的行业不参与。"""

    key = "hk_quality_value"
    label = "港股质量价值"
    market = "HK"
    style = "value"

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        min_roe: float = 12.0
        max_debt_ratio: float = 60.0
        min_fcf_years: int = 2
        max_margin_drop: float = 10.0      # 毛利率较上年同期下降超过该百分点视为不稳定（扣分）
        mos: float = 0.75
        take_profit: float = 1.2
        cheap_quantile: float = 0.2
        review_drop: float = 0.2
        tranches: int = 3
        pullback_add: float = 0.05
        min_weight: float = 0.05
        max_weight: float = 0.15
        candidates: int = 30

    def _metrics(self, u: dict, ctx: Context) -> tuple[dict | None, str | None]:
        """(指标, 质量不达标的原因)。行业不适用或缺报表时指标为 None。"""
        from ..screen import buckets

        info = u.get("info") or {}
        screen_cfg = ctx.app.settings.screen
        if not buckets.fcf_applicable(u, screen_cfg):
            return None, "行业不适用 FCF 口径"
        fm = buckets.floor_metrics(u, screen_cfg, ctx.hkdcny)
        if not fm:
            return None, "缺少报表"
        roe, debt, pe = info.get("roe"), info.get("debt_ratio"), u.get("pe_ttm")
        gm, gm0 = info.get("gross_margin"), info.get("gross_margin_prev")
        m = {"roe": roe, "debt_ratio": debt, "ey": 1 / pe if pe and pe > 0 else 0.0, "fcf_yield": fm["yield_cons"] or 0.0,
             "dividend_yield": info.get("dividend_yield") or 0.0, "fcf_years": fm["fcf_positive_years"],
             "margin_drop": (gm0 - gm) if gm is not None and gm0 is not None else None, "fair_price": fm["fair_price"],
             "net_cash_ratio": fm["net_cash_ratio"]}
        fail = None
        if roe is None or roe < self.p.min_roe:
            fail = "ROE 不达标"
        elif debt is not None and debt > self.p.max_debt_ratio and (fm["net_cash"] or 0) <= 0:
            fail = "负债率过高"
        elif fm["fcf_base"] is None or fm["fcf_base"] <= 0 or (fm["annual_n"] >= 2 and fm["fcf_positive_years"] < self.p.min_fcf_years):
            fail = "FCF 不稳定"
        elif any(f["severity"] == "high" for f in fm["flags"]):
            fail = "风险标记（FCF 不稳定或存贷双高）"
        return m, fail

    def qualifies(self, u: dict, ctx: Context) -> tuple[bool, str | None]:
        m, fail = self._metrics(u, ctx)
        return m is not None and fail is None, fail

    def score(self, ctx: Context) -> list[Target]:
        rows, reasons = [], {}

        def drop(why: str) -> None:
            reasons[why] = reasons.get(why, 0) + 1

        for u in ctx.universe:
            m, fail = self._metrics(u, ctx)
            if m is None:
                drop(fail)
                continue
            rows.append({"u": u, "ind": ctx.ind.get(u["secid"]), "fail": fail, **m})
        if not rows:
            ctx.notes.append(f"{self.label}：股票池没有可用的报表数据（先运行 stock screen refresh）")
            return []
        df = pd.DataFrame(rows)
        # 便宜程度在整个股票池（有报表的）里排名：盈利收益率与保守 FCF Yield 的排名平均，0 = 最便宜
        df["cheap_pct"] = ((_rank_pct(df["ey"]) + _rank_pct(df["fcf_yield"])) / 2).rank(pct=True)
        ok = []
        for _, r in df.iterrows():
            ind = r["ind"]
            if isinstance(r["fail"], str):
                drop(r["fail"])
            elif not isinstance(ind, dict) or not ind.get("turnover_med20") or ind["turnover_med20"] < ctx.min_turnover:
                drop("流动性不足或缺日线")
            elif ind["date"] < ctx.expected.isoformat() or ind["volume"] <= 0:
                drop("停牌或日线未更新")
            else:
                ok.append(r)
        ctx.notes.append(f"{self.label}：有报表 {len(df)} 只，质量与流动性筛选后 {len(ok)} 只（"
                         + "，".join(f"{k} {v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])) + "）")
        if not ok:
            return []
        q = pd.DataFrame(ok)
        quality = _rank_pct(q["roe"]) + 0.25 * q["margin_drop"].fillna(0).clip(lower=0) / max(self.p.max_margin_drop, 1e-9)
        q["score"] = 100 * (1 - (quality.rank(pct=True) + q["cheap_pct"].rank(pct=True)) / 2)
        out = []
        for _, r in q.sort_values("score", ascending=False).iterrows():
            u, ind = r["u"], r["ind"]
            tr = ctx.trackers.get(u["secid"])
            if _triggered(ctx, u["secid"]):
                continue
            if tr:
                if tr["ratio"] > self.p.mos:
                    continue
                gate = f"现价为目标价（情景加权估值）的 {tr['ratio']:.0%}（≤ {self.p.mos:.0%}）"
                bonus = 10 * (self.p.mos - tr["ratio"]) / self.p.mos
            else:
                if r["cheap_pct"] > self.p.cheap_quantile:
                    continue
                gate = f"便宜程度在港股通有报表的 {len(df)} 只中排前 {r['cheap_pct']:.0%}"
                bonus = 0.0
            reason = (f"ROE {r['roe']:.1f}%，盈利收益率 {r['ey']:.1%}，保守 FCF Yield {r['fcf_yield']:.1%}，"
                      f"股息率 {r['dividend_yield']:.1%}；{gate}")
            out.append(Target(secid=u["secid"], code=u["code"], name=u["name"], market="HK", score=round(float(r["score"]) + bonus, 1),
                              weight=0.0, price=ind["close"], entry={"type": "open"}, tranches=self.p.tranches, reason=reason,
                              sector=u.get("industry"), org_code=u.get("org_code"),
                              metrics={k: (None if pd.isna(r[k]) else float(r[k])) for k in
                                       ("roe", "ey", "fcf_yield", "dividend_yield", "cheap_pct", "fair_price")}
                              | {"target": tr["target"] if tr else None}))
            if len(out) >= self.p.candidates:
                break
        out.sort(key=lambda t: -t.score)
        if out:
            hi, lo = out[0].score, out[-1].score
            for t in out:
                x = (t.score - lo) / (hi - lo) if hi > lo else 0.5
                t.weight = round(self.p.min_weight + (self.p.max_weight - self.p.min_weight) * x, 4)
        return out

    def check_exit(self, pos: dict, ctx: Context) -> ExitSignal | None:
        if sig := kill_exit(pos, ctx):
            return sig
        ind = ctx.ind.get(pos["secid"])
        if not ind:
            return None
        price = ind["close"]
        tr = ctx.trackers.get(pos["secid"])
        u = ctx.row(pos["secid"])
        if tr:
            if price >= self.p.take_profit * tr["price_equiv_target"]:
                return ExitSignal("sell", 1.0, 4, f"现价达到目标价（情景加权估值）的 {price / tr['price_equiv_target']:.0%}，止盈")
        elif u is not None:
            m, _ = self._metrics(u, ctx)
            fair = m["fair_price"] if m else None
            if fair and price >= self.p.take_profit * fair:
                return ExitSignal("sell", 1.0, 4, f"现价达到合理价值（保守 FCF ÷ 8% + 净现金）的 {price / fair:.0%}，止盈")
        period = ((u or {}).get("info") or {}).get("report_date")
        seen = (pos.get("flags") or {}).get("period")
        if u is not None and (ctx.rebalance_day or (seen and period and period != seen)):
            ok, why = self.qualifies(u, ctx)
            if not ok:
                when = "调仓日复查" if ctx.rebalance_day else f"新财报（{period}）复查"
                return ExitSignal("sell", 1.0, 2, f"{when}：{why}，质量条件不再满足")
        if price <= pos["avg_cost"] * (1 - self.p.review_drop) and not (pos.get("flags") or {}).get("review"):
            return ExitSignal("review", 0.0, 2, f"较成本下跌 {price / pos['avg_cost'] - 1:.0%}，按纪律复核投资论点（不机械止损）", kind="note")
        return None

    def update(self, pos: dict, ctx: Context) -> dict:
        """记下最新报告期：新财报出来时 check_exit 会复查质量条件（「季度 + 财报后」）。"""
        out = super().update(pos, ctx)
        row = ctx.row(pos["secid"])
        period = ((row or {}).get("info") or {}).get("report_date")
        flags = pos.get("flags") or {}
        # 新报告期仍达标就记下；不达标时保留旧的，让 check_exit 按「新财报复查」卖出
        if period and flags.get("period") != period and (not flags.get("period") or self.qualifies(row, ctx)[0]):
            out["flags"] = {**flags, "period": period}
        return out

    def add_signal(self, pos: dict, ctx: Context) -> Target | None:
        if pos["tranches"] >= self.p.tranches or (pos.get("flags") or {}).get("review"):
            return None
        ind = ctx.ind.get(pos["secid"])
        if not ind:
            return None
        price = ind["close"]
        pullback = price <= pos["avg_cost"] * (1 - self.p.pullback_add)
        if not (ctx.rebalance_day or pullback) or price <= pos["avg_cost"] * (1 - self.p.review_drop):
            return None
        tr = ctx.trackers.get(pos["secid"])
        if tr and tr["ratio"] > self.p.mos:
            return None
        why = "回调到成本下方，按计划补下一批" if pullback else "调仓日补齐下一批"
        return Target(secid=pos["secid"], code=pos["code"], name=pos["name"], market="HK", score=pos.get("score") or 0.0,
                      weight=pos.get("target_weight") or self.p.min_weight, price=price, tranches=self.p.tranches,
                      reason=f"第 {pos['tranches'] + 1}/{self.p.tranches} 批：{why}", sector=pos.get("sector"), org_code=pos.get("org_code"))


# ---------------------------------------------------------------- A 股景气趋势（欧奈尔、米尼威尼 + 行业动量）


class ASectorTrend(Strategy):
    """A 股第二阶段上升趋势 + 行业动量。股票池是第一阶段后按市值取前 N 只（每晚刷新日线），相对强度在这批股票内排名。

    选：收盘 > 50 日线 > 150 日线 > 200 日线、200 日线比 20 天前高、距 52 周高点不超过 near_high、相对强度前 rs_top；
       行业（选股器的行业字段）按成员相对强度中位数排名，前 sector_top 的行业才选（行业成员不足时不用这一条）。
    买：放量突破之前 20 日的最高价（量比 ≥ breakout_volume，对 50 日均量）；已在前一天放量突破且未远离突破位的，开盘买。
       先买一半，站稳 add_after_days 个交易日且浮盈 ≥ add_min_gain 再买另一半；只加盈利的仓位。
    卖：硬止损 hard_stop 或 atr_mult 倍 ATR（取近者，不小于 min_stop）；浮盈 ≥ trail_after 后按最高收盘 − trail_atr 倍 ATR 移动止损
       （不低于成本）；跌破 50 日线减半；持有 time_stop_days 个交易日仍未上涨则时间止损。
    仓位：每笔风险 risk_per_trade × 净值 ÷ 止损距离，不超过单票上限。"""

    key = "a_sector_trend"
    label = "A 股景气趋势"
    market = "A"
    style = "trend"

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        rs_top: float = 0.2
        sector_top: float = 0.4
        min_sector_members: int = 3
        near_high: float = 0.25
        breakout_volume: float = 1.5
        breakout_limit: float = 0.03
        hard_stop: float = 0.075
        atr_mult: float = 2.0
        min_stop: float = 0.03
        risk_per_trade: float = 0.01
        trail_after: float = 0.10
        trail_atr: float = 2.5
        time_stop_days: int = 15
        time_stop_gain: float = 0.0
        add_after_days: int = 3
        add_min_gain: float = 0.02
        max_weight: float = 0.15
        candidates: int = 30

    def stop_for(self, level: float, atr: float | None) -> float:
        d = self.p.hard_stop
        if atr:
            d = min(d, max(self.p.atr_mult * atr / level, self.p.min_stop))
        return round(level * (1 - d), 2)

    def template(self, ind: dict) -> bool:
        ma50, ma150, ma200, ma200p = ind.get("ma50"), ind.get("ma150"), ind.get("ma200"), ind.get("ma200_prev")
        if None in (ma50, ma150, ma200, ma200p):
            return False
        return (ind["close"] > ma50 > ma150 > ma200 > ma200p
                and ind["close"] >= ind["high_250"] * (1 - self.p.near_high))

    def score(self, ctx: Context) -> list[Target]:
        pool, reasons = [], {}
        for u in ctx.universe:
            ind = ctx.ind.get(u["secid"])
            if not ind or ind["bars"] < 250:
                reasons["日线不足一年"] = reasons.get("日线不足一年", 0) + 1
                continue
            if ind["date"] < ctx.expected.isoformat() or ind["volume"] <= 0:
                reasons["停牌或日线未更新"] = reasons.get("停牌或日线未更新", 0) + 1
                continue
            if not ind.get("turnover_med20") or ind["turnover_med20"] < ctx.min_turnover:
                reasons["流动性不足"] = reasons.get("流动性不足", 0) + 1
                continue
            if ind.get("rs_raw") is None:
                continue
            pool.append((u, ind))
        if not pool:
            ctx.notes.append(f"{self.label}：没有可用日线（晚间任务 stock fund refresh 还没回填？）")
            return []
        df = pd.DataFrame([{"secid": u["secid"], "sector": u.get("industry") or "", "rs": ind["rs_raw"]} for u, ind in pool])
        df["rs_pct"] = df["rs"].rank(pct=True)                       # 1 = 最强
        med = df.groupby("sector")["rs_pct"].agg(["median", "count"])
        med = med[(med["count"] >= self.p.min_sector_members) & (med.index != "")]
        sector_pct = med["median"].rank(pct=True) if len(med) >= 3 else pd.Series(dtype=float)
        use_sector = len(sector_pct) > 0
        if not use_sector:
            ctx.notes.append(f"{self.label}：行业样本不足，未使用行业动量")
        rs_pct = dict(zip(df["secid"], df["rs_pct"]))
        trend_n = 0
        out = []
        for u, ind in pool:
            rp = rs_pct[u["secid"]]
            if rp < 1 - self.p.rs_top or not self.template(ind):
                continue
            trend_n += 1
            sp = float(sector_pct.get(u.get("industry") or "", float("nan"))) if use_sector else float("nan")
            if use_sector and not pd.isna(sp) and sp < 1 - self.p.sector_top:
                continue
            if _triggered(ctx, u["secid"]):
                continue
            pivot, close, vr = ind.get("pivot"), ind["close"], ind.get("vol_ratio") or 0
            if not pivot:
                continue
            if close > pivot * (1 + self.p.breakout_limit):
                continue                                             # 已远离突破位，不追
            if close > pivot and vr >= self.p.breakout_volume:
                entry = {"type": "open", "level": round(close, 2), "limit": round(pivot * (1 + self.p.breakout_limit), 2)}
                how = f"昨日放量突破 {pivot:.2f}（量比 {vr:.1f}）"
                level = close
            elif close <= pivot:
                entry = {"type": "breakout", "level": round(pivot, 2), "volume_ratio": self.p.breakout_volume,
                         "limit": round(pivot * (1 + self.p.breakout_limit), 2)}
                how = f"等待放量突破 {pivot:.2f}"
                level = pivot
            else:
                continue                                             # 价格在突破位上方但没放量：不追
            stop = self.stop_for(level, ind.get("atr"))
            weight = min(self.p.risk_per_trade / max(1 - stop / level, 1e-6), self.p.max_weight)
            sp_txt = f"，行业动量前 {1 - sp:.0%}" if not pd.isna(sp) else ""
            score = 100 * (0.7 * rp + 0.3 * (sp if not pd.isna(sp) else 0.5))
            out.append(Target(secid=u["secid"], code=u["code"], name=u["name"], market="A", score=round(score, 1),
                              weight=round(weight, 4), price=close, stop=stop, entry=entry, tranches=2,
                              reason=f"第二阶段上升趋势，相对强度前 {1 - rp:.0%}{sp_txt}；{how}", sector=u.get("industry"),
                              org_code=u.get("org_code"),
                              metrics={"rs_pct": rp, "sector_pct": None if pd.isna(sp) else sp, "pivot": pivot, "vol_ratio": vr,
                                       "atr": ind.get("atr"), "ma50": ind.get("ma50"), "ma200": ind.get("ma200")}))
        ctx.notes.append(f"{self.label}：日线股票池 {len(pool)} 只，符合趋势模板 {trend_n} 只，入选 {len(out)} 只")
        out.sort(key=lambda t: -t.score)
        return out[:self.p.candidates]

    def update(self, pos: dict, ctx: Context) -> dict:
        ind = ctx.ind.get(pos["secid"])
        if not ind:
            return {}
        high = max(pos.get("high_close") or 0.0, ind["close"])
        stop = pos.get("stop") or self.stop_for(pos["entry_price"], ind.get("atr"))
        if high >= pos["entry_price"] * (1 + self.p.trail_after) and ind.get("atr"):
            stop = max(stop, round(high - self.p.trail_atr * ind["atr"], 2), round(pos["entry_price"], 2))
        return {"high_close": high, "stop": stop}

    def check_exit(self, pos: dict, ctx: Context) -> ExitSignal | None:
        if sig := kill_exit(pos, ctx):
            return sig
        ind = ctx.ind.get(pos["secid"])
        if not ind:
            return None
        close, stop = ind["close"], pos.get("stop")
        if stop and close <= stop:
            return ExitSignal("sell", 1.0, 1, f"收盘 {close:.2f} 跌破止损 {stop:.2f}")
        held = calendar.trading_days_between("A", date.fromisoformat(pos["opened_on"]), date.fromisoformat(ind["date"][:10]))
        if held >= self.p.time_stop_days and close <= pos["entry_price"] * (1 + self.p.time_stop_gain):
            return ExitSignal("sell", 1.0, 1, f"持有 {held} 个交易日仍未上涨，时间止损")
        flags = pos.get("flags") or {}
        if ind.get("ma50") and close < ind["ma50"] and not flags.get("half_off"):
            return ExitSignal("trim", 0.5, 3, f"收盘 {close:.2f} 跌破 50 日线 {ind['ma50']:.2f}，减半")
        return None

    def add_signal(self, pos: dict, ctx: Context) -> Target | None:
        if pos["tranches"] >= 2:
            return None
        ind = ctx.ind.get(pos["secid"])
        if not ind:
            return None
        held = calendar.trading_days_between("A", date.fromisoformat(pos["opened_on"]), date.fromisoformat(ind["date"][:10]))
        if held < self.p.add_after_days or ind["close"] < pos["entry_price"] * (1 + self.p.add_min_gain):
            return None
        return Target(secid=pos["secid"], code=pos["code"], name=pos["name"], market="A", score=pos.get("score") or 0.0,
                      weight=pos.get("target_weight") or 0.0, price=ind["close"], stop=pos.get("stop"), tranches=2,
                      reason=f"突破后站稳 {held} 个交易日、浮盈 {ind['close'] / pos['entry_price'] - 1:.1%}，买第二批",
                      sector=pos.get("sector"), org_code=pos.get("org_code"))


STRATEGIES: dict[str, type[Strategy]] = {s.key: s for s in (HKQualityValue, ASectorTrend)}
