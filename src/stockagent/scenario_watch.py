"""情景切换检测（代码计算，不调用模型）：追踪中的公司，现价进入了牛 / 基准 / 熊哪个情景的价格区间。

- 归属：价格落进某个情景的区间（ScenarioPoint.low/high，按当前情景价同比缩放，缩放方式同 valuation.risk_stats）就属于它；
  程序定价的情景只有单点（low = high），按情景价 ±POINT_BAND 取一个隐含区间；区间重叠时取情景价最近的；
  落在区间之间的空隙，按相邻两个区间边界的中点归给较近的一个（assign，用于首次记录与展示）——任何价格都归属于牛 / 基准 / 熊之一。
- 切换（zone）：只有价格真正进入另一个情景的区间才算切换；已处于某情景、价格落在它与相邻情景之间的空隙时，仍算原情景
  （否则单点情景下，价格越过两价格的中点就会收到「进入熊市区间」，而价格其实离熊价还很远）。
  进入新区间要越过边界 BUFFER（1%）才算，避免在边界附近来回抖动。
- 只对定价上市地判断（model.listing）。状态存 kv `scen_zone:<公司 id>`（情景、所用修正 id、价格），第一次只记录不通知。
- 切换写入 scenario_events（页面横幅，可「知道了」），并推送【情景切换】；估值修正后情景归属变化也推送，标注「估值修正后」。
- 触发时机：每次行情刷新（quotes.py）之后；stock run 抓取行情之后、标的追踪之后；stock target run 修正之后。
"""

from __future__ import annotations

import json

from . import notify
from .config import Company
from .utils import iso, log, pct, utcnow
from .valuation import (
    SCENARIO_LABEL, SCENARIOS, STANCE_LABEL, TARGET_LABEL, ValuationModel, display_price, fmt, load_model, refresh_fx,
)

BUFFER = 0.01           # 越过边界多少（相对价格）才算切换
POINT_BAND = 0.10       # 单点情景（程序定价）的隐含区间：情景价上下各多少
KV_PREFIX = "scen_zone:"
DRIVER_CHARS = 240      # 推送里新进入情景的驱动说明最多多少字；其他情景更短
OTHER_DRIVER_CHARS = 70
DISCLAIMER = "情景加权估值，非投资建议"


def intervals(model: ValuationModel) -> dict[str, tuple[float, float, float]]:
    """各情景的价格区间 {情景: (低, 高, 情景价)}：原区间按「当前情景价 ÷ 原区间中点」同比缩放；没有区间（单点）则取情景价 ± POINT_BAND。"""
    out = {}
    for s in model.scenarios:
        v = model.price(s)
        lo, hi = s.low, s.high
        if lo is not None and hi is not None and hi > lo > 0:
            k = v / ((lo + hi) / 2)
            lo, hi = lo * k, hi * k
        else:
            lo, hi = v * (1 - POINT_BAND), v * (1 + POINT_BAND)
        out[s.name] = (lo, hi, v)
    return out


def assign(model: ValuationModel, price: float) -> str:
    """price 属于哪个情景（无滞后）。"""
    iv = intervals(model)
    inside = [n for n, (lo, hi, _) in iv.items() if lo <= price <= hi]
    if inside:
        return min(inside, key=lambda n: abs(iv[n][2] - price))
    below = [(hi, n) for n, (_, hi, _) in iv.items() if hi < price]   # 不在任何区间内：取下方、上方最近的边界，按中点分
    above = [(lo, n) for n, (lo, _, _) in iv.items() if lo > price]
    if below and above:
        hi, n_lo = max(below)
        lo, n_hi = min(above)
        return n_lo if price < (hi + lo) / 2 else n_hi
    return max(below)[1] if below else min(above)[1]


def zone(model: ValuationModel, price: float, prev: str | None) -> str:
    """带滞后的归属：已在 prev 情景时，价格要真正进入另一个情景的区间（越过边界 BUFFER）才切换；
    落在两个区间之间的空隙里、prev 是空隙两侧之一时，仍算 prev。"""
    z = assign(model, price)
    iv = intervals(model)
    if prev is None or prev not in iv or z == prev:
        return z
    if not any(lo <= price <= hi for lo, hi, _ in iv.values()):
        below = [n for n, (_, hi, _) in iv.items() if hi < price]
        above = [n for n, (lo, _, _) in iv.items() if lo > price]
        if below and above and prev in (max(below, key=lambda n: iv[n][1]), min(above, key=lambda n: iv[n][0])):
            return prev
        return z
    # 价格在 z 的区间里：把价格朝 prev 所在的方向推 BUFFER，仍在 z 的区间里才算真的进入
    toward = 1 + BUFFER if iv[prev][2] > price else 1 - BUFFER
    lo, hi, _ = iv[z]
    return z if lo <= price * toward <= hi else prev


def live_model(app, company: Company, rev: dict) -> tuple[ValuationModel, object]:
    """某条修正的模型（按最新汇率）与它定价上市地的 Listing。"""
    model, _ = refresh_fx(app, load_model(rev["model"]))
    listing = next((l for l in company.listings if l.key == model.listing), company.pricing_listing)
    return model, listing


def snapshot(model: ValuationModel, price: float, mode: str) -> dict:
    """事件里存的情景快照：各情景价格、相对现价的空间、关键驱动，以及目标价。"""
    by = {s.name: s for s in model.scenarios}
    return {"scenarios": {n: {"price": model.price(s), "upside": model.price(s) / price - 1, "drivers": s.drivers_text or ""}
                          for n, s in by.items()},
            "target": model.target(mode), "target_upside": model.target(mode) / price - 1, "currency": model.price_currency}


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1] + "…"


def push_text(company: Company, ev: dict) -> tuple[str, str]:
    """推送的标题与正文。ev 是 scenario_events 的一行（prices 已解析）。"""
    snap, new = ev["snapshot"], ev["new_scenario"]
    ccy = snap["currency"]
    lab = SCENARIO_LABEL
    head = (f"估值修正后，现价所处情景由{lab[ev['old_scenario']]}变为{lab[new]}" if ev["reason"] == "valuation"
            else f"{lab[ev['old_scenario']]} → {lab[new]}（价格进入{lab[new]}情景区间）")
    lines = [f"{company.name}（{ev['listing']}）：{head}",
             f"现价 {fmt(ev['price'])} {ccy}" + (f"，日涨跌 {pct(ev['change_pct'])}" if ev["change_pct"] is not None else "")]
    lines.append("情景价格（相对现价）：" + "｜".join(
        f"{lab[n]} {fmt(x['price'])}（{pct(x['upside'], 0)}）" for n in SCENARIOS if (x := snap["scenarios"].get(n))))
    lines.append(f"{TARGET_LABEL}：{fmt(snap['target'])} {ccy}（{pct(snap['target_upside'], 0)}）")
    if ev.get("stance"):
        lines.append(f"报告立场：{STANCE_LABEL.get(ev['stance'], ev['stance'])}")
    for n in SCENARIOS:
        x = snap["scenarios"].get(n)
        if not x:
            continue
        if n == new:
            lines.append(f"▶ 新进入的{lab[n]}情景驱动：{_clip(x['drivers'], DRIVER_CHARS) or '（报告未给出）'}")
        elif x["drivers"]:
            lines.append(f"{lab[n]}情景驱动：{_clip(x['drivers'], OTHER_DRIVER_CHARS)}")
    lines.append(f"（{DISCLAIMER}）")
    return f"【情景切换】{company.name} {lab[ev['old_scenario']]}→{lab[new]}" + ("（估值修正后）" if ev["reason"] == "valuation" else ""), "\n".join(lines)


def check(app, companies: list[Company], push: bool = True) -> list[str]:
    """逐家检查追踪中的公司；返回日志（切换了才有内容）。价格取 display_price（盘中最新价或收盘价），过期的盘中价不判断。"""
    out: list[str] = []
    mode = app.settings.tracker.target
    for c in companies:
        tr = app.db.tracker(c.id)
        if not tr or not tr["enabled"] or not tr["revision_id"]:
            continue
        rev = app.db.revision(tr["revision_id"])
        try:
            model, listing = live_model(app, c, rev)
            px = display_price(app, listing)
            if not px["price"] or px["stale"] or len(model.scenarios) < 2:
                continue
            price = px["price"]
            key = KV_PREFIX + c.id
            raw = app.db.kv_get(key)
            state = json.loads(raw) if raw else None
            prev = state["zone"] if state else None
            new = zone(model, price, prev)
            if state and state.get("rev") == rev["id"] and new == prev:
                continue
            if state is None or new == prev:  # 第一次只记录；情景没变只更新所用修正
                app.db.kv_set(key, json.dumps({"zone": new, "rev": rev["id"], "price": price, "at": iso(utcnow())}))
                continue
            reason = "price"
            if state.get("rev") != rev["id"]:  # 修正换过：旧模型在这个价格上是否也会切换，决定是价格引起的还是估值修正引起的
                old = app.db.revision(state["rev"]) if state.get("rev") else None
                old_zone = None
                if old:
                    try:
                        old_zone = zone(live_model(app, c, old)[0], price, prev)
                    except Exception:  # noqa: BLE001 - 旧修正解析不了就当作估值修正
                        pass
                reason = "price" if old_zone == new else "valuation"
            ev = {"company_id": c.id, "listing": listing.display, "old_scenario": prev, "new_scenario": new, "price": price,
                  "change_pct": px["change_pct"], "snapshot": snapshot(model, price, mode), "reason": reason,
                  "revision_id": rev["id"], "stance": rev.get("stance") or (app.db.latest_dossier(c.id) or {}).get("card", {}).get("stance")}
            ev["id"] = app.db.add_scenario_event(ev)
            app.db.kv_set(key, json.dumps({"zone": new, "rev": rev["id"], "price": price, "at": iso(utcnow())}))
            title, text = push_text(c, ev)
            out.append(f"{c.name}：{SCENARIO_LABEL[prev]} → {SCENARIO_LABEL[new]}（现价 {fmt(price)}，{'估值修正后' if reason == 'valuation' else '价格'}）")
            if push:
                out += [f"  推送：{r}" for r in notify.push(app.settings, title, text)] or ["  未配置推送渠道（见 config.yaml notify）"]
        except Exception as e:  # noqa: BLE001 - 一家出错不影响其他公司
            log.warning("%s 情景切换检测失败：%s", c.name, e)
            out.append(f"✗ {c.name} 情景切换检测：{type(e).__name__}: {str(e)[:120]}")
    return out
