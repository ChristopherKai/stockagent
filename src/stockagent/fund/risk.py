"""共用风控：把策略的目标权重变成可执行的操作建议。所有基金一视同仁，多个上限同时生效时取最小值。

顺序：退出信号（止损 > 论点失效 > 风控降仓 > 止盈 > 替换）→ 总仓位上限（市场状态 × 回撤熔断）→ 超配调回（调仓日）
→ 分批加仓 → 新建仓（单票、行业、流动性、跨基金同一发行人、最小仓位、名额与替换缓冲带）→ 按交易单位取整、先卖后买。
权重偏离不到 drift 的不调。价格、权重、股数全部由代码计算。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from . import costs
from .config import RiskConfig
from .regime import LABEL as REGIME_LABEL
from .strategy import ExitSignal, Target

ACTION_LABEL = {"buy": "建仓", "add": "加仓", "trim": "减仓", "sell": "清仓", "review": "复核", "stop": "止损单"}
BREAKER_LABEL = {0: "正常", 1: "回撤一档：新仓减半", 2: "回撤二档：停止开新仓、总仓位降到 50% 以下", 3: "回撤三档：基金暂停（需手动恢复）"}


@dataclass
class Book:
    """风控需要的账户视图（策略看不到）。金额都是基金本币。"""

    fund: dict                                      # fund 表的行
    cash: float
    positions: dict[str, dict]                      # secid → 持仓
    prices: dict[str, float]                        # secid → 参考价（最新收盘）
    equity: float = 0.0

    def __post_init__(self) -> None:
        if not self.equity:
            self.equity = self.cash + sum(p["shares"] * self.prices.get(s, p["avg_cost"]) for s, p in self.positions.items())

    def weight(self, secid: str) -> float:
        p = self.positions.get(secid)
        return p["shares"] * self.prices.get(secid, p["avg_cost"]) / self.equity if p and self.equity > 0 else 0.0


@dataclass
class Limits:
    regime_cap: float                               # 市场状态对应的总仓位上限
    breaker: int                                    # 回撤熔断档位
    new_entries: int | None                         # 今天最多新开几笔（None 不限，0 不开）
    rebalance_day: bool
    capacity: dict[str, float] = field(default_factory=dict)     # secid → 流动性可容纳金额（本币）
    other_funds: dict[str, float] = field(default_factory=dict)  # 发行人 → 其他基金持有市值（人民币）
    total_cny: float = 0.0                                        # 全部基金合计资产（人民币）
    fx_cny: float = 1.0                                           # 1 单位本币 = ? 人民币
    lots: dict[str, tuple[int, bool]] = field(default_factory=dict)  # code → (每手股数, 是否确知)


@dataclass
class Plan:
    suggestions: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    cap: float = 1.0
    unchanged: int = 0                               # 不动的持仓数
    position_updates: dict[str, dict] = field(default_factory=dict)


def group_key(x) -> str:
    """跨基金敞口按发行人合并（A+H 同一家公司算一份）。"""
    org = x.get("org_code") if isinstance(x, dict) else x.org_code
    sid = x.get("secid") if isinstance(x, dict) else x.secid
    return f"org:{org}" if org else sid


def exposure_cap(fund: dict, risk: RiskConfig, regime_state: str, breaker: int) -> float:
    cap = risk.exposure[fund["style"]][regime_state]
    if breaker >= 3:
        cap = min(cap, risk.halt_exposure)
    elif breaker >= 2:
        cap = min(cap, risk.breaker_cap)
    return cap


def new_entry_quota(fund: dict, risk: RiskConfig, regime_state: str, breaker: int, status: str) -> int | None:
    if status != "paper" or breaker >= 2:
        return 0
    if fund["style"] == "trend":
        return {"attack": None, "neutral": risk.neutral_new_entries, "defend": 0}[regime_state]
    return None


def breaker_level(current: int, drawdown: float, thresholds: tuple[float, float, float], recover: tuple[float, float]) -> int:
    """回撤熔断档位（带回滞）：第三档只能手动恢复；第二档在回撤收窄到 recover[1] 内降档，第一档在 recover[0] 内解除。"""
    d1, d2, d3 = thresholds
    if current >= 3 or drawdown >= d3:
        return 3
    level = 2 if drawdown >= d2 else 1 if drawdown >= d1 else 0
    if current == 2 and level < 2:
        level = 2 if drawdown > recover[1] else (1 if drawdown > recover[0] else 0)
    elif current == 1 and level < 1:
        level = 1 if drawdown > recover[0] else 0
    return max(level, 0)


def _round(book: Book, lim: Limits, secid: str, code: str, value: float, price: float) -> tuple[int, int, bool]:
    lot, known = lim.lots.get(code, costs.lot_size(book.fund["market"], code))
    return costs.round_shares(book.fund["market"], code, value / price if price > 0 else 0, lot), lot, known


def build(book: Book, risk: RiskConfig, lim: Limits, exits: list[tuple[dict, ExitSignal]], adds: list[tuple[dict, Target]],
          targets: list[Target], day: str, regime_state: str, size_factor: float | None = None,
          on_skip: Callable[[str, str], None] | None = None) -> Plan:
    f, E = book.fund, book.equity
    plan = Plan()
    if E <= 0:
        plan.notes.append("基金资产为零，不生成建议")
        return plan
    skip = on_skip or (lambda code, why: plan.notes.append(f"{code}：{why}"))
    cap = exposure_cap(f, risk, regime_state, lim.breaker)
    plan.cap = cap
    factor = size_factor if size_factor is not None else (risk.breaker_size_factor if lim.breaker == 1 else 1.0)
    sells: list[dict] = []
    buys: list[dict] = []
    remaining = {s: dict(p) for s, p in book.positions.items()}   # 卖出后剩余的持仓（股数）

    def sug(p_or_t, action: str, kind: str, shares: int, cur_w: float, tgt_w: float, reason: str, priority: int, **kw) -> dict:
        get = (lambda k: p_or_t.get(k)) if isinstance(p_or_t, dict) else (lambda k: getattr(p_or_t, k, None))
        return {"fund_id": f["id"], "date": day, "secid": get("secid"), "code": get("code"), "name": get("name"), "action": action,
                "kind": kind, "cur_weight": round(cur_w, 4), "target_weight": round(tgt_w, 4), "shares": shares, "reason": reason,
                "priority": priority, "valid_until": day, **kw}

    # 1. 退出信号
    for pos, sig in sorted(exits, key=lambda x: x[1].priority):
        sid = pos["secid"]
        w = book.weight(sid)
        if sig.action == "review":
            sells.append(sug(pos, "review", "note", 0, w, w, sig.reason, sig.priority, status="note"))
            continue
        lot, _ = lim.lots.get(pos["code"], costs.lot_size(f["market"], pos["code"]))
        n = int(pos["shares"]) if sig.fraction >= 1 else costs.round_shares(f["market"], pos["code"], pos["shares"] * sig.fraction, lot)
        if n <= 0:
            skip(pos["code"], f"{sig.reason}，但减仓数量不足一手，保留")
            continue
        if n >= pos["shares"]:
            n, action = int(pos["shares"]), "sell"
        else:
            action = "trim"
        new_w = w * (1 - n / pos["shares"])
        sells.append(sug(pos, action, "open", n, w, new_w, sig.reason, sig.priority, limit_price=None))
        remaining[sid]["shares"] -= n
        if remaining[sid]["shares"] <= 0:
            del remaining[sid]

    def value_of(sid: str) -> float:
        p = remaining.get(sid)
        return p["shares"] * book.prices.get(sid, p["avg_cost"]) if p else 0.0

    # 2. 总仓位超过上限（市场状态转弱或熔断）：按比例降仓，超出不到 drift 的不动
    exposure = sum(value_of(s) for s in remaining) / E
    if exposure > cap + risk.drift:
        scale = cap / exposure
        for sid, p in list(remaining.items()):
            v = value_of(sid)
            n, lot, _ = _round(book, lim, sid, p["code"], v * (1 - scale), book.prices.get(sid, p["avg_cost"]))
            if n <= 0:
                continue
            n = min(n, int(p["shares"]))
            w = v / E
            sells.append(sug(p, "sell" if n >= p["shares"] else "trim", "open", n, w, w * (1 - n / p["shares"]),
                             f"总仓位 {exposure:.0%} 超过上限 {cap:.0%}（{BREAKER_LABEL[lim.breaker] if lim.breaker else '市场状态转弱'}），按比例降仓",
                             3))
            p["shares"] -= n
            if p["shares"] <= 0:
                del remaining[sid]
        exposure = sum(value_of(s) for s in remaining) / E

    # 3. 调仓日：单票超过上限 drift 以上的调回上限
    if lim.rebalance_day:
        for sid, p in list(remaining.items()):
            w = value_of(sid) / E
            if w > f["max_weight"] + risk.drift:
                n, _, _ = _round(book, lim, sid, p["code"], (w - f["max_weight"]) * E, book.prices[sid])
                if n > 0:
                    sells.append(sug(p, "trim", "open", n, w, f["max_weight"], f"权重 {w:.1%} 超过单票上限 {f['max_weight']:.0%}，调回", 5))
                    p["shares"] -= n
        exposure = sum(value_of(s) for s in remaining) / E

    proceeds = sum(s["shares"] * book.prices.get(s["secid"], 0) for s in sells if s["kind"] == "open")
    cash_left = book.cash + proceeds * (1 - 0.002)          # 预留卖出费用
    sector_w: dict[str, float] = {}
    for sid, p in remaining.items():
        sector_w[p.get("sector") or ""] = sector_w.get(p.get("sector") or "", 0.0) + value_of(sid) / E

    def headroom(t, full_w: float) -> tuple[float, str | None]:
        """单票、流动性、跨基金、行业四个上限取最小（按计划的满仓权重算）。返回 (可用权重, 卡住的原因)。"""
        caps = {"单票上限": f["max_weight"], "计划权重": full_w}
        if (c := lim.capacity.get(t.secid)) is not None:
            caps["流动性容量"] = c / E
        if lim.total_cny > 0:
            other = lim.other_funds.get(group_key(t), 0.0)
            caps["跨基金同一发行人"] = (risk.cross_fund_cap * lim.total_cny - other) / (E * lim.fx_cny)
        sec = t.sector or ""
        if sec:
            caps["行业上限"] = f["sector_cap"] - sector_w.get(sec, 0.0) + (book.weight(t.secid) if t.secid in remaining else 0.0)
        why = min(caps, key=caps.get)
        return max(caps[why], 0.0), why

    # 4. 分批加仓（不受新开仓名额限制；熔断二档以上、趋势基金防守市况不加）
    no_new_risk = lim.breaker >= 2 or f["status"] != "paper" or (f["style"] == "trend" and regime_state == "defend")
    for pos, t in adds:
        sid = pos["secid"]
        if no_new_risk or sid not in remaining:
            continue
        cur_w = value_of(sid) / E
        room, why = headroom(t, t.weight)
        step = t.weight / max(t.tranches, 1) * factor
        add_w = min(step, room - cur_w, cap - exposure)
        if add_w < 0.005:
            skip(pos["code"], f"加仓空间不足（{why}）")
            continue
        price = book.prices.get(sid) or t.price
        n, _, known = _round(book, lim, sid, pos["code"], min(add_w * E, cash_left), price)
        if n <= 0:
            skip(pos["code"], "加仓金额不足一手")
            continue
        amt = n * price
        cash_left -= amt * 1.002
        exposure += amt / E
        sector_w[t.sector or ""] = sector_w.get(t.sector or "", 0.0) + amt / E
        buys.append(sug(pos, "add", "open", n, cur_w, cur_w + amt / E, t.reason, 6, stop=pos.get("stop"),
                        limit_price=round(price * 1.02, 3), trigger={"full_weight": t.weight, "lot_known": known}))

    # 5. 新建仓
    held_codes = set(remaining)
    slots = f["max_holdings"] - len(remaining)
    quota = lim.new_entries
    opened = 0
    weakest = sorted(((p.get("score") or 0.0, s) for s, p in remaining.items()), key=lambda x: x[0])
    for t in targets:
        if t.secid in book.positions or t.secid in held_codes:
            continue
        if quota is not None and opened >= quota:
            if quota == 0:
                plan.notes.append("今日不开新仓（" + ("回撤熔断" if lim.breaker >= 2 else "基金暂停" if f["status"] != "paper"
                                                    else f"市场状态{REGIME_LABEL[regime_state]}") + "）")
            else:
                plan.notes.append(f"中性市况只开最强的 {quota} 笔")
            break
        replace = None
        if slots <= 0:
            # 替换只在调仓日、且只换给开盘买入的候选（条件单可能不触发，不能先卖掉持仓）
            if not lim.rebalance_day or not weakest or t.entry.get("type") == "breakout":
                continue
            w_score, w_sid = weakest[0]
            if t.score < w_score * (1 + risk.replace_margin):
                continue
            replace = w_sid
        room, why = headroom(t, t.weight)
        full_w = min(t.weight, room)
        if full_w < f["min_weight"] - 1e-9:
            skip(t.code, f"可用权重 {full_w:.1%} 低于最小仓位 {f['min_weight']:.0%}（受{why}限制）")
            continue
        first_w = full_w / max(t.tranches, 1) * factor
        avail = cap - exposure + (value_of(replace) / E if replace else 0.0)
        first_w = min(first_w, avail)
        level = t.entry.get("level") or t.price
        cash_now = cash_left + (value_of(replace) if replace else 0.0)
        n, lot, known = _round(book, lim, t.secid, t.code, min(first_w * E, cash_now), level)
        if n <= 0:
            skip(t.code, "总仓位上限或现金不足，或不足一手")
            continue
        if replace:
            p = remaining.pop(replace)
            v = p["shares"] * book.prices.get(replace, p["avg_cost"])
            sells.append(sug(p, "sell", "open", int(p["shares"]), v / E, 0.0,
                             f"被更好的候选替换：{t.name}（{t.score:.0f} 分，比持仓最弱的 {w_score:.0f} 分高 {risk.replace_margin:.0%} 以上）", 5))
            weakest.pop(0)
            cash_left += v * (1 - 0.002)
            exposure -= v / E
        else:
            slots -= 1
        amt = n * level
        cash_left -= amt * 1.002
        exposure += amt / E
        sector_w[t.sector or ""] = sector_w.get(t.sector or "", 0.0) + full_w
        opened += 1
        held_codes.add(t.secid)
        conditional = t.entry.get("type") == "breakout"
        tranche = f"首批 1/{t.tranches}，目标 {full_w:.1%}" if t.tranches > 1 else f"目标 {full_w:.1%}"
        buys.append(sug(t, "buy", "conditional" if conditional else "open", n, 0.0, amt / E, t.reason, 6, stop=t.stop,
                        limit_price=t.entry.get("limit") or round(t.price * 1.02, 3),
                        trigger={**t.entry, "full_weight": full_w, "tranche": tranche, "lot": lot, "lot_known": known,
                                 "score": t.score, "sector": t.sector, "org_code": t.org_code, "tranches": t.tranches}))

    # 6. 持仓的止损条件单（盘中检查用）：今天没有卖出计划的
    selling = {s["secid"] for s in sells if s["action"] in ("sell",)}
    for sid, p in remaining.items():
        if sid in selling or not p.get("stop"):
            continue
        sells.append(sug(p, "stop", "conditional", int(p["shares"]), value_of(sid) / E, 0.0, f"跌破 {p['stop']:.2f} 止损", 1,
                         stop=p["stop"], trigger={"type": "stop", "price": p["stop"]}))
    touched = {s["secid"] for s in sells + buys if s["action"] not in ("stop", "review")}
    plan.unchanged = sum(1 for s in book.positions if s not in touched)
    plan.suggestions = sorted(sells, key=lambda s: s["priority"]) + buys
    return plan
