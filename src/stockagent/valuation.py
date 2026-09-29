"""结构化估值模型：把深度报告的敏感性矩阵重建成可修正的情景模型，目标价全部由代码计算。

深度报告里跟踪卡的情景价格区间来自研究 agent 调用 valuation_matrix 算出的格点
（隐含股价 =（倍数 × 驱动指标 + 净现金）÷ 股本 × 汇率）。这里从工具日志中找回那次调用，
为每个情景定出（驱动指标, 倍数）点，使代码算出的情景股价恰好等于卡片区间中值——
基线不经过 LLM，之后的修正也只改假设，股价与目标价始终由代码重算。
"""

from __future__ import annotations

import json
import math
import re
import statistics
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from .tools import matrix_price

ScenarioName = Literal["bull", "base", "bear"]
SCENARIOS = ("bull", "base", "bear")
SCENARIO_LABEL = {"bull": "牛市", "base": "基准", "bear": "熊市"}
CURRENCY_LABEL = {"CNY": "人民币", "HKD": "港元", "USD": "美元"}
FX_SECID = {"HKD": "133.HKDCNH", "USD": "133.USDCNH"}  # 1 单位外币 = close 人民币

MATCH_TOL = 0.015       # 卡片情景端点与矩阵格点的相对误差容忍度（报告里的价格常被四舍五入）
SHARES_TOL = 0.3        # 矩阵股本与「行情市值 ÷ 现价」的偏差超过 30% 视为单位错误
FX_STALE_DAYS = 5       # 汇率数据超过 5 天视为不可用
TARGET_LABEL = "目标价（情景加权估值）"


class ScenarioPoint(BaseModel):
    name: ScenarioName
    driver: float
    multiple: float
    probability: float
    low: float | None = None       # 深度报告原区间（只用于展示）
    high: float | None = None
    drivers_text: str = ""         # 跟踪卡中的关键驱动说明


class ValuationModel(BaseModel):
    """method=matrix：情景股价 =（倍数 × 驱动 + 净现金）÷ 股本 × 汇率；
    method=range：没有可用矩阵时的退路，driver 即情景价格，只能按比例修正。"""

    method: Literal["matrix", "range"] = "matrix"
    driver_label: str
    multiple_label: str
    shares: float                  # 亿股
    net_cash: float = 0.0          # 与驱动指标同一单位（报告币种）
    fx: float = 1.0                # 1 单位报告币种折合多少股价币种
    report_currency: str = ""      # 空字符串表示无法判断（不刷新汇率）
    price_currency: str
    listing: str                   # 定价上市地（跟踪卡的第一上市地）
    source: str = ""
    scenarios: list[ScenarioPoint] = Field(min_length=1)

    def scenario(self, name: str) -> ScenarioPoint | None:
        return next((s for s in self.scenarios if s.name == name), None)

    def price(self, s: ScenarioPoint) -> float:
        if self.method == "range":
            return max(0.0, s.driver)
        return max(0.0, matrix_price(s.driver, s.multiple, self.shares, self.net_cash, self.fx))

    def prices(self) -> dict[str, float]:
        return {s.name: self.price(s) for s in self.scenarios}

    def probabilities(self) -> dict[str, float]:
        """归一化后的概率（模型给出的概率合计不为 1 时按比例缩放；全为 0 时平均分配）。"""
        total = sum(max(0.0, s.probability) for s in self.scenarios)
        if total <= 0:
            return {s.name: 1 / len(self.scenarios) for s in self.scenarios}
        return {s.name: max(0.0, s.probability) / total for s in self.scenarios}

    def expected(self) -> float:
        probs = self.probabilities()
        return sum(probs[s.name] * self.price(s) for s in self.scenarios)

    def target(self, mode: str = "expected") -> float:
        if mode == "base" and (b := self.scenario("base")):
            return self.price(b)
        return self.expected()

    def normalized(self) -> "ValuationModel":
        probs = self.probabilities()
        m = self.model_copy(deep=True)
        for s in m.scenarios:
            s.probability = probs[s.name]
        m.scenarios.sort(key=lambda s: SCENARIOS.index(s.name))
        return m


def load_model(data: str | dict | None) -> ValuationModel | None:
    if not data:
        return None
    return ValuationModel.model_validate(json.loads(data) if isinstance(data, str) else data)


# ---------------------------------------------------------------- 汇率

def _fx_series(app, currency: str):
    if currency == "CNY":
        return None
    bars = app.bars(FX_SECID[currency], limit=4000)
    return None if bars is None else bars[["date", "close"]]


def fx_rate(app, from_ccy: str, to_ccy: str, on: date | None = None) -> tuple[float, str] | None:
    """1 单位 from_ccy 折合多少 to_ccy，以及所用汇率的日期。用港元、美元兑离岸人民币日线在同一天换算交叉汇率；
    on 之前 FX_STALE_DAYS 天内没有数据则返回 None。"""
    if from_ccy not in CURRENCY_LABEL or to_ccy not in CURRENCY_LABEL:
        return None
    if from_ccy == to_ccy:
        return 1.0, (on or date.today()).isoformat()
    on = on or date.today()
    series = {c: _fx_series(app, c) for c in {from_ccy, to_ccy} if c != "CNY"}
    if any(s is None or s.empty for s in series.values()):
        return None
    common = None
    for s in series.values():
        dates = set(s["date"][s["date"] <= on.isoformat()])
        common = dates if common is None else common & dates
    if not common:
        return None
    day = max(common)
    if (on - date.fromisoformat(day)).days > FX_STALE_DAYS:
        return None

    def cny_per(c: str) -> float:
        if c == "CNY":
            return 1.0
        s = series[c]
        return float(s.loc[s["date"] == day, "close"].iloc[-1])

    return cny_per(from_ccy) / cny_per(to_ccy), day


_CCY_HINTS = [("USD", r"美元|USD|US\$"), ("HKD", r"港元|港币|HKD"), ("CNY", r"人民币|CNY|RMB|元")]


def infer_report_currency(app, fx: float, price_currency: str, on: date, label: str = "") -> str:
    """矩阵的 fx 是「报告币种 → 股价币种」的换算率：与代码按当日汇率算出的候选比对，取最接近的币种；
    比对不上（缺汇率数据）时再看驱动指标名称里的币种字样。"""
    best, err = "", 1.0
    for ccy in CURRENCY_LABEL:
        r = fx_rate(app, ccy, price_currency, on)
        if r and r[0] > 0:
            e = abs(fx / r[0] - 1)
            if e < err:
                best, err = ccy, e
    if best and err <= 0.03:
        return best
    if abs(fx - 1) < 1e-9:
        return price_currency
    for ccy, pattern in _CCY_HINTS:
        if re.search(pattern, label):
            return ccy
    return ""


def refresh_fx(app, model: ValuationModel, on: date | None = None) -> tuple[ValuationModel, str | None]:
    """按最新汇率刷新模型；返回 (新模型, 变动说明)。无法判断报告币种或缺汇率数据时原样返回。"""
    if model.method == "range" or not model.report_currency:
        return model, None
    r = fx_rate(app, model.report_currency, model.price_currency, on)
    if r is None:
        return model, None
    rate, day = r
    if abs(rate / model.fx - 1) < 1e-4:  # 变化不到万分之一（多为四舍五入差异），不算变动
        return model, None
    m = model.model_copy(update={"fx": rate})
    note = (f"汇率 {model.fx:.4f} → {rate:.4f}（1 {CURRENCY_LABEL[model.report_currency]} 折合"
            f"{CURRENCY_LABEL[model.price_currency]}，{day}）")
    return m, note


# ---------------------------------------------------------------- 从深度报告重建基线

def _cells(args: dict) -> dict[tuple[float, float], float] | None:
    try:
        shares, net_cash, fx = float(args["shares"]), float(args.get("net_cash") or 0), float(args.get("fx") or 1)
        drivers = [float(d) for d in args["driver_values"]]
        multiples = [float(m) for m in args["multiples"]]
    except (KeyError, TypeError, ValueError):
        return None
    if shares <= 0 or not drivers or not multiples:
        return None
    return {(d, m): matrix_price(d, m, shares, net_cash, fx) for d in drivers for m in multiples}


def _year(label: str) -> int | None:
    m = re.search(r"(20\d{2})\s*[EeFf年A]?", label)
    return int(m.group(1)) if m else None


def quote_shares(quote: dict | None) -> float | None:
    """行情快照推算的总股本（亿股）：市值 ÷ 现价。"""
    if not quote or not quote.get("market_cap") or not quote.get("price"):
        return None
    return quote["market_cap"] / quote["price"] / 1e8


def matrix_candidates(tool_log: list[dict], shares_ref: float | None) -> tuple[list[dict], list[str]]:
    """工具日志中可用的 valuation_matrix 调用；返回 (候选, 跳过的原因)。"""
    out, skipped = [], []
    for i, x in enumerate(tool_log):
        if x.get("tool") != "valuation_matrix":
            continue
        tag = f"第 {i + 1} 次工具调用"
        if str(x.get("result", "")).startswith("错误"):
            skipped.append(f"{tag}：工具报错")
            continue
        cells = _cells(x.get("args") or {})
        if not cells:
            skipped.append(f"{tag}：参数不完整")
            continue
        if max(abs(v) for v in cells.values()) < 0.01:
            skipped.append(f"{tag}：格点全为 0（股本 {x['args'].get('shares')} 可能写成了股数而不是亿股）")
            continue
        shares = float(x["args"]["shares"])
        if shares_ref and abs(shares / shares_ref - 1) > SHARES_TOL:
            skipped.append(f"{tag}：股本 {shares:g} 亿股与行情推算的 {shares_ref:.2f} 亿股不符")
            continue
        out.append({"index": i, "args": x["args"], "cells": cells})
    return out, skipped


def match_endpoints(cells: dict, scenarios: list[dict], tol: float = MATCH_TOL) -> dict[str, list[tuple[float, float]]]:
    """每个情景的 price_low / price_high 命中了哪些格点（驱动, 倍数）。"""
    hits: dict[str, list[tuple[float, float]]] = {}
    for s in scenarios:
        for end in ("price_low", "price_high"):
            v = s.get(end)
            if not v:
                continue
            key, cell = min(cells.items(), key=lambda kv: abs(kv[1] - v))
            if abs(cell - v) <= abs(v) * tol:
                hits.setdefault(s["name"], []).append(key)
    return hits


def _range_model(card_scenarios: list[dict], listing: str, currency: str, source: str) -> ValuationModel:
    pts = [ScenarioPoint(name=s["name"], driver=(s["price_low"] + s["price_high"]) / 2, multiple=1.0,
                         probability=s.get("probability") or 0, low=s["price_low"], high=s["price_high"],
                         drivers_text=s.get("drivers", "")) for s in card_scenarios]
    return ValuationModel(method="range", driver_label="情景价格（报告区间中值）", multiple_label="—", shares=1.0,
                          net_cash=0.0, fx=1.0, report_currency=currency, price_currency=currency, listing=listing,
                          source=source, scenarios=pts).normalized()


def build_baseline(app, company, dossier: dict) -> tuple[ValuationModel, list[str]]:
    """由深度报告（跟踪卡的情景 + 工具日志里的估值矩阵）在代码中重建估值模型，不调用 LLM。
    返回 (模型, 标注)。标注提示需要人工留意的地方（股本存疑、驱动年份已到、只能按比例修正等）。"""
    card = dossier["card"]
    tool_log = dossier.get("tool_log") or json.loads(dossier.get("tool_log_json") or "[]")
    listing = company.listings[0]
    price_ccy = listing.currency
    scen = [s for s in card.get("scenarios", []) if s.get("name") in SCENARIOS and s.get("price_low") and s.get("price_high")]
    seen: set[str] = set()
    scen = [s for s in scen if not (s["name"] in seen or seen.add(s["name"]))]
    flags: list[str] = []
    did = dossier.get("id")
    if not scen:
        raise ValueError(f"深度报告 #{did} 的跟踪卡没有情景价格，无法建立估值基线")

    shares_ref = quote_shares(app.db.quote(app.secid(listing)))
    if shares_ref is None:
        flags.append(f"没有 {listing.display} 的行情快照，未能核对估值矩阵的股本")
    calls, skipped = matrix_candidates(tool_log, shares_ref)
    best, best_hits = None, {}
    for c in calls:  # 命中最多的调用；并列时取靠后的（通常是修正过参数的那次）
        hits = match_endpoints(c["cells"], scen)
        if sum(map(len, hits.values())) >= sum(map(len, best_hits.values())):
            best, best_hits = c, hits
    n_hits = sum(map(len, best_hits.values()))
    if best is None or n_hits < 2:
        why = "；".join(skipped) or ("深度报告没有调用估值矩阵" if not calls else "情景价格与矩阵格点对不上")
        flags.append(f"未找到能解释情景价格的估值矩阵（{why}），改用区间模型：只能按比例修正情景价格")
        return _range_model(scen, listing.key, price_ccy, f"深度报告 #{did} · 跟踪卡情景区间"), flags

    a = best["args"]
    shares, net_cash, fx = float(a["shares"]), float(a.get("net_cash") or 0), float(a.get("fx") or 1)
    created = str(dossier.get("created_at", ""))[:10]
    on = date.fromisoformat(created) if created else date.today()
    report_ccy = infer_report_currency(app, fx, price_ccy, on, a.get("driver_label", ""))
    if not report_ccy:
        flags.append(f"无法判断报告币种（矩阵汇率 {fx:g}），之后不按最新汇率刷新")
    drivers_all = [float(d) for d in a["driver_values"]]
    points = []
    for s in scen:
        mid = (s["price_low"] + s["price_high"]) / 2
        matched = [d for d, _ in best_hits.get(s["name"], [])]
        driver = statistics.mean(matched) if matched else statistics.median(drivers_all)
        # 反解倍数，使情景股价恰好等于区间中值
        multiple = (mid / fx * shares - net_cash) / driver if driver else float("nan")
        if not math.isfinite(multiple) or multiple <= 0:
            flags.append(f"{SCENARIO_LABEL[s['name']]}情景反解出的倍数不合理（{multiple:.2f}），改用区间模型")
            return _range_model(scen, listing.key, price_ccy, f"深度报告 #{did} · 跟踪卡情景区间"), flags
        points.append(ScenarioPoint(name=s["name"], driver=driver, multiple=multiple, probability=s.get("probability") or 0,
                                    low=s["price_low"], high=s["price_high"], drivers_text=s.get("drivers", "")))
    model = ValuationModel(
        method="matrix", driver_label=a.get("driver_label", "驱动指标"), multiple_label=a.get("multiple_label", "倍数"),
        shares=shares, net_cash=net_cash, fx=fx, report_currency=report_ccy, price_currency=price_ccy, listing=listing.key,
        source=f"深度报告 #{did} · 第 {best['index'] + 1} 次工具调用 valuation_matrix（命中 {n_hits}/{2 * len(scen)} 个情景端点）",
        scenarios=points,
    ).normalized()
    if (y := _year(model.driver_label)) and y <= date.today().year:
        flags.append(f"驱动指标年份（{y}）已到，披露年报后应重跑深度报告，把驱动滚动到下一年")
    return model, flags


# ---------------------------------------------------------------- 修正路径

PATH_OPS = ("set", "add", "mul")


def paths(model: ValuationModel) -> list[str]:
    """模型中可以修正的路径（供 LLM 的 JSON schema 做枚举）。"""
    names = [s.name for s in model.scenarios]
    out = [f"{n}.driver" for n in names] + ["all.driver"]
    if model.method == "matrix":
        out += [f"{n}.multiple" for n in names] + ["all.multiple", "g.shares", "g.net_cash"]
    out += [f"prob.{n}" for n in names]
    return out


FACT_PATHS = ("g.shares", "g.net_cash")


def path_label(model: ValuationModel, path: str) -> str:
    head, _, attr = path.partition(".")
    if head == "g":
        return {"shares": "总股本（亿股）", "net_cash": "净现金"}[attr]
    if head == "prob":
        return f"{SCENARIO_LABEL.get(attr, attr)}情景概率"
    who = "全部情景" if head == "all" else SCENARIO_LABEL.get(head, head)
    what = model.driver_label if attr == "driver" else f"{model.multiple_label} 倍数"
    return f"{who}·{what}"


def get_values(model: ValuationModel, path: str) -> list[tuple[str, float]]:
    """路径当前的取值：[(具体路径, 值)]；all.* 展开为每个情景。"""
    head, _, attr = path.partition(".")
    if head == "g":
        return [(path, getattr(model, attr))]
    if head == "prob":
        s = model.scenario(attr)
        return [(path, s.probability)] if s else []
    targets = model.scenarios if head == "all" else [s for s in model.scenarios if s.name == head]
    return [(f"{s.name}.{attr}", getattr(s, attr)) for s in targets]


def set_value(model: ValuationModel, path: str, value: float) -> None:
    head, _, attr = path.partition(".")
    if head == "g":
        setattr(model, attr, value)
    elif head == "prob":
        model.scenario(attr).probability = value
    else:
        setattr(model.scenario(head), attr, value)


def apply_op(old: float, op: str, operand: float) -> float:
    if op == "set":
        return operand
    if op == "add":
        return old + operand
    if op == "mul":
        return old * operand
    raise ValueError(f"未知操作 {op}")


def check_value(model: ValuationModel, path: str, value: float) -> str | None:
    """新值的合法性；返回错误说明，合法返回 None。"""
    if not math.isfinite(value):
        return "结果不是有限数值"
    attr = path.partition(".")[2]
    if path == "g.shares" and value <= 0:
        return "总股本必须为正"
    if attr == "multiple" and value <= 0:
        return "倍数必须为正"
    if path.startswith("prob.") and not 0 <= value <= 1:
        return "概率应在 0–1 之间"
    if model.method == "range" and attr == "driver" and value <= 0:
        return "情景价格必须为正"
    return None


# ---------------------------------------------------------------- 展示

def fmt(v: float | None, digits: int = 2) -> str:
    return "—" if v is None or not math.isfinite(v) else f"{v:,.{digits}f}"


def describe(model: ValuationModel, target_mode: str = "expected") -> str:
    """估值模型的 Markdown 表（提示词、CLI 与报告附录共用）。"""
    ccy = model.price_currency
    probs = model.probabilities()
    if model.method == "matrix":
        head = (f"估值方法：（{model.multiple_label} × {model.driver_label} + 净现金 {fmt(model.net_cash)}）÷ 总股本 "
                f"{fmt(model.shares, 4)} 亿股 × 汇率 {fmt(model.fx, 4)}"
                + (f"（1 {CURRENCY_LABEL.get(model.report_currency, model.report_currency)} 折合 "
                   f"{fmt(model.fx, 4)} {CURRENCY_LABEL.get(ccy, ccy)}）" if model.report_currency else ""))
        rows = [f"| 情景 | {model.driver_label} | {model.multiple_label} | 股价（{ccy}） | 概率 | 报告原区间 | 关键驱动 |",
                "|---|---|---|---|---|---|---|"]
        rows += [f"| {SCENARIO_LABEL[s.name]} | {fmt(s.driver)} | {fmt(s.multiple)} | {fmt(model.price(s))} | {probs[s.name]:.0%} | "
                 f"{fmt(s.low)}–{fmt(s.high)} | {s.drivers_text.replace('|', '/')} |" for s in model.scenarios]
    else:
        head = "估值方法：区间模型（报告没有可用的估值矩阵，情景价格取报告区间中值，只能按比例修正）"
        rows = [f"| 情景 | 股价（{ccy}） | 概率 | 报告原区间 | 关键驱动 |", "|---|---|---|---|---|"]
        rows += [f"| {SCENARIO_LABEL[s.name]} | {fmt(model.price(s))} | {probs[s.name]:.0%} | {fmt(s.low)}–{fmt(s.high)} | "
                 f"{s.drivers_text.replace('|', '/')} |" for s in model.scenarios]
    tail = (f"{TARGET_LABEL}：{fmt(model.target(target_mode))} {ccy}"
            + ("（概率加权）" if target_mode == "expected" else "（基准情景）") + f"；来源：{model.source}")
    return "\n".join([head, "", *rows, "", tail])


def equivalents(app, model: ValuationModel, company, target: float) -> list[str]:
    """其他上市地的等值价格（按汇率折算，不含 AH 溢价）。美股 ADS 需要换股比例，暂不展示。"""
    out = []
    for l in company.listings:
        if l.key == model.listing or l.market == "US":
            continue
        r = fx_rate(app, model.price_currency, l.currency)
        if r:
            out.append(f"{l.display} 等值 {fmt(target * r[0])} {l.currency}（按汇率折算，未计 AH 溢价）")
    return out
