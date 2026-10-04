"""模拟账本：按日线成交、扣费、更新持仓与现金、记每日净值。纯计算部分不碰数据库，便于 --dry-run 与测试。

成交规则：
- 盘前建议（kind=open）按当日开盘价成交（含滑点）；A 股开盘即涨停且全天一价（一字板）买不到，一字跌停卖不出；
  开盘价高于限价的买单不成交。
- 条件单（kind=conditional）：突破买单在当日最高价 ≥ 突破位且全天量比达标时，按 max(开盘价, 突破位) 成交（不超过限价）；
  止损单在最低价 ≤ 止损位时按 min(开盘价, 止损位) 成交（跳空低开按开盘价）。
- 先卖后买；买单受当时现金约束（向下取整到交易单位）。
- 行情是前复权日线，股息没有记入现金：除权除息后的净值偏保守（第一期的已知局限）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import costs
from .config import CostConfig


@dataclass
class Fill:
    suggestion: dict
    status: str                      # filled | unfilled | expired
    price: float | None = None
    shares: int = 0
    fees: float = 0.0
    realized: float | None = None
    note: str = ""


@dataclass
class State:
    fund: dict
    cash: float
    positions: dict[str, dict]
    fills: list[Fill] = field(default_factory=list)


def _bar_ok(bar: dict | None, day: str) -> bool:
    return bool(bar) and str(bar["date"])[:10] == day and (bar.get("volume") or 0) > 0


def try_fill(s: dict, bar: dict | None, day: str, market: str, prev_close: float | None, avg_vol50: float | None) -> tuple[str, float | None, str]:
    """一条建议在当天能否成交：(状态, 未计滑点的成交价, 说明)。"""
    if not _bar_ok(bar, day):
        return "unfilled", None, "当日停牌或没有日线"
    o, h, lo = float(bar["open"]), float(bar["high"]), float(bar["low"])
    buy = s["action"] in ("buy", "add")
    one_price = abs(h - lo) < 1e-9
    if market == "A" and prev_close:
        up, down = costs.limit_prices(s.get("code") or "", prev_close, s.get("name") or "")
        if buy and one_price and o >= up - 1e-9:
            return "unfilled", None, "一字涨停，买不到"
        if not buy and one_price and o <= down + 1e-9:
            return "unfilled", None, "一字跌停，卖不出（次日计划会继续给出卖出）"
    trig = s.get("trigger") or {}
    limit = s.get("limit_price")
    if s["kind"] == "open":
        if buy and limit and o > limit + 1e-9:
            return "unfilled", None, f"开盘价 {o:.3f} 高于限价 {limit:.3f}，未成交"
        return "filled", o, "按开盘价成交"
    if trig.get("type") == "breakout":
        level, vr = float(trig["level"]), float(trig.get("volume_ratio") or 0)
        if h < level:
            return "expired", None, "当日未突破，作废"
        if vr and avg_vol50 and float(bar["volume"]) < vr * avg_vol50:
            return "expired", None, f"突破但量能不足（量比 {float(bar['volume']) / avg_vol50:.1f} < {vr}），作废"
        px = max(o, level)
        if limit and px > limit + 1e-9:
            return "expired", None, f"高开 {o:.3f} 超过限价 {limit:.3f}，不追"
        return "filled", px, f"突破 {level:.3f} 成交"
    if trig.get("type") == "stop":
        stop = float(trig["price"])
        if lo > stop:
            return "expired", None, "未触及止损"
        return "filled", min(o, stop), f"触及止损 {stop:.3f}"
    return "expired", None, "无法识别的条件单"


def apply(state: State, s: dict, px: float, cost: CostConfig, day: str, lot: int) -> Fill:
    """按成交价（未计滑点）执行一条建议，更新现金与持仓。"""
    side = "buy" if s["action"] in ("buy", "add") else "sell"
    price = costs.fill_price(cost, side, px)
    sid = s["secid"]
    pos = state.positions.get(sid)
    if side == "sell":
        if not pos:
            return Fill(s, "expired", note="已无持仓")
        n = int(min(s["shares"], pos["shares"]))
        amount = n * price
        fee = costs.fees(cost, "sell", amount)
        realized = amount - fee - n * pos["avg_cost"]
        state.cash += amount - fee
        pos["shares"] -= n
        pos["last_trade_on"] = day
        if s["action"] == "trim" and "50 日线" in (s.get("reason") or ""):
            pos.setdefault("flags", {})["half_off"] = day
        if pos["shares"] <= 0:
            del state.positions[sid]
        return Fill(s, "filled", price, n, fee, realized)
    n = int(s["shares"])
    while n > 0 and n * price + costs.fees(cost, "buy", n * price) > state.cash + 1e-6:
        n -= lot
    if n <= 0:
        return Fill(s, "unfilled", note="现金不足")
    amount = n * price
    fee = costs.fees(cost, "buy", amount)
    state.cash -= amount + fee
    trig = s.get("trigger") or {}
    if pos:
        total = pos["shares"] + n
        pos["avg_cost"] = (pos["shares"] * pos["avg_cost"] + amount + fee) / total
        pos["shares"] = total
        pos["tranches"] = (pos.get("tranches") or 1) + 1
        pos["last_trade_on"] = day
    else:
        state.positions[sid] = {
            "secid": sid, "code": s.get("code"), "name": s.get("name"), "sector": trig.get("sector"), "org_code": trig.get("org_code"),
            "shares": n, "avg_cost": (amount + fee) / n, "entry_price": price, "opened_on": day, "stop": s.get("stop"),
            "high_close": price, "tranches": 1, "target_weight": trig.get("full_weight"), "score": trig.get("score"),
            "thesis": s.get("reason"), "flags": {} if trig.get("lot_known", True) else {"lot_unknown": True}, "last_trade_on": day,
        }
    return Fill(s, "filled", price, n, fee)


def settle(state: State, pending: list[dict], bars: dict[str, dict], prev_close: dict[str, float], avg_vol: dict[str, float],
           cost: CostConfig, day: str, lots: dict[str, int]) -> list[Fill]:
    """当天收盘后结算所有待执行建议：先卖后买。bars 为 secid → 当日日线（dict）。"""
    order = sorted(pending, key=lambda s: (0 if s["action"] in ("sell", "trim", "stop") else 1, s.get("priority") or 9, s.get("id") or 0))
    fills = []
    sold_today = set()
    for s in order:
        if s["kind"] == "note":
            continue
        if s["action"] == "stop" and s["secid"] in sold_today:
            fills.append(Fill(s, "expired", note="已按计划卖出"))
            continue
        status, px, note = try_fill(s, bars.get(s["secid"]), day, state.fund["market"], prev_close.get(s["secid"]), avg_vol.get(s["secid"]))
        if status != "filled":
            fills.append(Fill(s, status, note=note))
            continue
        f = apply(state, s, px, cost, day, lots.get(s.get("code") or "", 100))
        f.note = f.note or note
        if f.status == "filled" and s["action"] in ("sell", "stop"):
            sold_today.add(s["secid"])
        fills.append(f)
    state.fills += fills
    return fills


def nav_row(state: State, closes: dict[str, float], day: str, prev: dict | None, bench_close: float | None,
            bench_base: float | None, units: float | None = None) -> dict:
    """当日净值。closes 为持仓的收盘价（缺失时沿用成本价并在调用方提示）。units 为总份额（有资金进出时不等于初始资金）。"""
    f = state.fund
    mv = sum(p["shares"] * closes.get(s, p["avg_cost"]) for s, p in state.positions.items())
    equity = state.cash + mv
    nav = equity / (units or f["initial_capital"])
    peak = max(prev["peak"] if prev else 1.0, nav)
    return {"fund_id": f["id"], "date": day, "nav": nav, "equity": equity, "cash": state.cash,
            "exposure": mv / equity if equity > 0 else 0.0, "peak": peak, "drawdown": 1 - nav / peak if peak > 0 else 0.0,
            "positions": len(state.positions), "benchmark_close": bench_close,
            "benchmark_nav": bench_close / bench_base if bench_close and bench_base else None}


def trade_stats(trades: list[dict]) -> dict:
    """已实现的卖出：胜率、盈亏比、每笔期望值（本币）。"""
    closed = [t["realized"] for t in trades if t["side"] == "sell" and t.get("realized") is not None]
    wins = [x for x in closed if x > 0]
    losses = [-x for x in closed if x <= 0]
    n = len(closed)
    win_rate = len(wins) / n if n else None
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    return {"n": n, "win_rate": win_rate, "avg_win": avg_win, "avg_loss": avg_loss,
            "payoff": avg_win / avg_loss if avg_loss > 0 else None,
            "expectancy": (win_rate * avg_win - (1 - win_rate) * avg_loss) if win_rate is not None else None}


def max_drawdown(navs: list[float]) -> float:
    peak, mdd = 0.0, 0.0
    for v in navs:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, 1 - v / peak)
    return mdd
