"""结构化估值模型：把深度报告的敏感性矩阵重建成可修正的情景模型，目标价全部由代码计算。

深度报告里跟踪卡的情景价格区间来自研究 agent 调用 valuation_matrix 算出的格点
（股权口径：隐含股价 = 倍数 × 驱动指标 ÷ 股本 × 汇率；EV 口径再加净现金）。这里从工具日志中找回那次调用，
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

from .tools import MATRIX_BASES, infer_basis, matrix_price

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
    """method=matrix：情景股价 = 倍数 × 驱动 ÷ 股本 × 汇率（basis=equity，PE、PB 等股权倍数）
    或（倍数 × 驱动 + 净现金）÷ 股本 × 汇率（basis=ev，EV 倍数）；
    method=range：没有可用矩阵时的退路，driver 即情景价格，只能按比例修正。"""

    method: Literal["matrix", "range"] = "matrix"
    # 旧基线（区分口径之前）一律按「加净现金」算出卡片区间，缺省 ev 使其数值不变
    basis: Literal["equity", "ev"] = "ev"
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
    anchor: dict | None = None     # 深度报告写作时的程序参考价（anchor.compute），只用于提示偏离，不参与计算

    def scenario(self, name: str) -> ScenarioPoint | None:
        return next((s for s in self.scenarios if s.name == name), None)

    def price(self, s: ScenarioPoint) -> float:
        if self.method == "range":
            return max(0.0, s.driver)
        return max(0.0, matrix_price(s.driver, s.multiple, self.shares, self.net_cash, self.fx, self.basis))

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

def effective_args(entry: dict) -> dict:
    """工具日志里一次 valuation_matrix 调用实际计算用的参数：模型传入的参数，加上工具记录的口径与（程序补上行列后的）全部格点。"""
    return {**(entry.get("args") or {}), **(entry.get("meta") or {})}


def basis_options(args: dict) -> list[str]:
    """这次矩阵调用可能的口径。工具明确记录了口径就只用它；区分口径之前的旧调用一律加了净现金（ev），
    但新调用在模型没填 basis 时按倍数名称推断——两种都试，按命中卡片端点的多少选，推断的口径排在后面（并列时优先）。"""
    if args.get("basis") in MATRIX_BASES:
        return [args["basis"]]
    guess = infer_basis(args.get("multiple_label", ""))
    if not float(args.get("net_cash") or 0):
        return [guess]
    return [b for b in MATRIX_BASES if b != guess] + [guess]


def _cells(args: dict, basis: str = "ev") -> dict[tuple[float, float], float] | None:
    try:
        shares, net_cash, fx = float(args["shares"]), float(args.get("net_cash") or 0), float(args.get("fx") or 1)
        drivers = [float(d) for d in args["driver_values"]]
        multiples = [float(m) for m in args["multiples"]]
    except (KeyError, TypeError, ValueError):
        return None
    if shares <= 0 or not drivers or not multiples:
        return None
    return {(d, m): matrix_price(d, m, shares, net_cash, fx, basis) for d in drivers for m in multiples}


def _year(label: str) -> int | None:
    m = re.search(r"(20\d{2})\s*[EeFf年A]?", label)
    return int(m.group(1)) if m else None


def quote_shares(quote: dict | None, listing=None) -> float | None:
    """行情快照推算的总股本（亿股）：市值 ÷ 现价。美股 ADS 的行情按 ADS 计，乘以每 ADS 股数换成普通股。"""
    if not quote or not quote.get("market_cap") or not quote.get("price"):
        return None
    ratio = (listing.ads_ratio or 1.0) if listing is not None and listing.market == "US" else 1.0
    return quote["market_cap"] / quote["price"] / 1e8 * ratio


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
        args = effective_args(x)
        options = basis_options(args)
        cells = _cells(args, options[-1])
        if not cells:
            skipped.append(f"{tag}：参数不完整")
            continue
        if max(abs(v) for v in cells.values()) < 0.01:
            skipped.append(f"{tag}：格点全为 0（股本 {args.get('shares')} 可能写成了股数而不是亿股）")
            continue
        shares = float(args["shares"])
        if shares_ref and abs(shares / shares_ref - 1) > SHARES_TOL:
            skipped.append(f"{tag}：股本 {shares:g} 亿股与行情推算的 {shares_ref:.2f} 亿股不符")
            continue
        out += [{"index": i, "args": args, "basis": b, "cells": _cells(args, b)} for b in options]
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


def endpoint_candidates(cells: dict, scenario: dict, tol: float = MATCH_TOL) -> list[float]:
    """情景的 price_low / price_high 在容差内命中的所有格点的驱动值（同一价格常对应多个格点）。"""
    out = set()
    for end in ("price_low", "price_high"):
        v = scenario.get(end)
        if v:
            out |= {d for (d, _), cell in cells.items() if abs(cell - v) <= abs(v) * tol}
    return sorted(out)


_PLAIN_NUM = re.compile(r"(?<![A-Za-z0-9.])\d[\d,]*(?:\.\d+)?")  # 前面是汉字也算数字的开头（\w 会匹配汉字）


def narrative_drivers(text: str, drivers: list[float]) -> list[float]:
    """情景描述里写明、且是矩阵某一行取值的驱动指标（如「2027E 收入 1,000–1,100 亿元」中的 1000 与 1100）。"""
    out = set()
    for raw in _PLAIN_NUM.findall(text or ""):
        x = raw.replace(",", "")
        digits = len(x.split(".")[1]) if "." in x else 0
        out |= {d for d in drivers if d and round(d, digits) == float(x)}  # 按写出的位数比较：1,206 不算 1200
    return sorted(out)


def choose_drivers(scen: list[dict], cells: dict, closest: dict[str, list[tuple[float, float]]],
                   drivers_all: list[float]) -> tuple[dict[str, float], list[str]]:
    """每个情景的驱动指标：优先用情景描述里写明的矩阵行；否则在命中的格点里牛市取最大、熊市取最小、基准取最近格点的均值；
    再保证 牛 ≥ 基准 ≥ 熊（驱动都为正时）——同一价格可由「高驱动 × 低倍数」或「低驱动 × 高倍数」得到，
    只按价格对格点会出现熊市收入高于基准这类与情景叙述相反的拆分。返回 ({情景: 驱动}, 标注)。"""
    chosen, cands, flags = {}, {}, []
    for s in scen:
        name = s["name"]
        told = narrative_drivers(s.get("drivers", ""), drivers_all)
        hit = endpoint_candidates(cells, s)
        cands[name] = sorted(set(told) | set(hit))
        if told:
            chosen[name] = statistics.mean(told)
        elif hit:
            near = [d for d, _ in closest.get(name, [])]
            chosen[name] = max(hit) if name == "bull" else min(hit) if name == "bear" else statistics.mean(near or hit)
        else:
            chosen[name] = statistics.median(drivers_all)
    base = chosen.get("base")
    if base is None or any(v <= 0 for v in chosen.values()):
        return chosen, flags
    for name, ok, pick in (("bear", lambda v: v <= base, max), ("bull", lambda v: v >= base, min)):
        if name in chosen and not ok(chosen[name]):
            good = [v for v in cands[name] if ok(v)]
            old = chosen[name]
            chosen[name] = pick(good) if good else base
            flags.append(f"{SCENARIO_LABEL[name]}情景的驱动指标由 {old:g} 调整为 {chosen[name]:g}，以保持 牛 ≥ 基准 ≥ 熊"
                         + ("" if good else "（命中的格点里没有满足顺序的取值，取基准情景的值）"))
    return chosen, flags


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
    # 情景价格按报告当时的定价上市地写成；旧卡没有记录，当时按第一个上市地定价
    listing = next((l for l in company.listings if l.key == card.get("price_listing")), company.listings[0])
    price_ccy = listing.currency
    scen = [s for s in card.get("scenarios", []) if s.get("name") in SCENARIOS and s.get("price_low") and s.get("price_high")]
    seen: set[str] = set()
    scen = [s for s in scen if not (s["name"] in seen or seen.add(s["name"]))]
    flags: list[str] = []
    did = dossier.get("id")
    if not scen:
        raise ValueError(f"深度报告 #{did} 的跟踪卡没有情景价格，无法建立估值基线")

    shares_ref = quote_shares(app.db.quote(app.secid(listing)), listing)
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

    a, basis = best["args"], best["basis"]
    shares, net_cash, fx = float(a["shares"]), float(a.get("net_cash") or 0), float(a.get("fx") or 1)
    created = str(dossier.get("created_at", ""))[:10]
    on = date.fromisoformat(created) if created else date.today()
    report_ccy = infer_report_currency(app, fx, price_ccy, on, a.get("driver_label", ""))
    if not report_ccy:
        flags.append(f"无法判断报告币种（矩阵汇率 {fx:g}），之后不按最新汇率刷新")
    drivers_all = [float(d) for d in a["driver_values"]]
    chosen, order_flags = choose_drivers(scen, best["cells"], best_hits, drivers_all)
    flags += order_flags
    points = []
    for s in scen:
        mid = (s["price_low"] + s["price_high"]) / 2
        driver = chosen[s["name"]]
        # 反解倍数，使情景股价恰好等于区间中值
        multiple = (mid / fx * shares - (net_cash if basis == "ev" else 0.0)) / driver if driver else float("nan")
        if not math.isfinite(multiple) or multiple <= 0:
            flags.append(f"{SCENARIO_LABEL[s['name']]}情景反解出的倍数不合理（{multiple:.2f}），改用区间模型")
            return _range_model(scen, listing.key, price_ccy, f"深度报告 #{did} · 跟踪卡情景区间"), flags
        points.append(ScenarioPoint(name=s["name"], driver=driver, multiple=multiple, probability=s.get("probability") or 0,
                                    low=s["price_low"], high=s["price_high"], drivers_text=s.get("drivers", "")))
    model = ValuationModel(
        method="matrix", basis=basis, driver_label=a.get("driver_label", "驱动指标"), multiple_label=a.get("multiple_label", "倍数"),
        shares=shares, net_cash=net_cash, fx=fx, report_currency=report_ccy, price_currency=price_ccy, listing=listing.key,
        source=f"深度报告 #{did} · 第 {best['index'] + 1} 次工具调用 valuation_matrix（命中 {n_hits}/{2 * len(scen)} 个情景端点）",
        scenarios=points, anchor=card.get("anchor"),
    ).normalized()
    if basis == "ev" and net_cash and infer_basis(model.multiple_label) == "equity":
        flags.append(f"矩阵对 {model.multiple_label} 这类股权倍数也加了净现金 {net_cash:g}（区分口径之前的旧算法），"
                     "情景价格按报告原样保留；重跑深度报告后按股权口径计算")
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
        out += [f"{n}.multiple" for n in names] + ["all.multiple", "g.shares"] + (["g.net_cash"] if model.basis == "ev" else [])
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


# ---------------------------------------------------------------- 情景的风险收益统计与参考立场

STANCE_LABEL = {"bullish": "偏多", "neutral": "中性", "bearish": "偏空"}


def _excess(lo: float, hi: float, c: float) -> float:
    """X ~ 均匀分布 [lo, hi]：E[max(X − c, 0)]。"""
    if c <= lo:
        return (lo + hi) / 2 - c
    return 0.0 if c >= hi else (hi - c) ** 2 / (2 * (hi - lo))


def _shortfall(lo: float, hi: float, c: float) -> float:
    """X ~ 均匀分布 [lo, hi]：E[max(c − X, 0)]。"""
    if c >= hi:
        return c - (lo + hi) / 2
    return 0.0 if c <= lo else (c - lo) ** 2 / (2 * (hi - lo))


def risk_stats(model: ValuationModel, price: float | None) -> dict | None:
    """情景表的风险收益统计（代码计算）：
    - 加权空间 = 概率加权价格 ÷ 现价 − 1；基准情景空间 = 基准情景价 ÷ 现价 − 1；
    - 跌破现价的概率：每个情景按报告原区间均匀分布（区间按「当前情景价 ÷ 原区间中点」同比缩放，没有区间时视为单点）；
    - 上行/下行比 = 高于现价部分的期望幅度 ÷ 低于现价部分的期望幅度。"""
    if not price or price <= 0:
        return None
    probs = model.probabilities()
    up = down = below = 0.0
    for s in model.scenarios:
        p, v = probs[s.name], model.price(s)
        lo, hi = s.low, s.high
        if lo is not None and hi is not None and hi > lo > 0:
            k = v / ((lo + hi) / 2)
            lo, hi = lo * k, hi * k
            below += p * min(max((price - lo) / (hi - lo), 0.0), 1.0)
            up, down = up + p * _excess(lo, hi, price), down + p * _shortfall(lo, hi, price)
        else:
            below += p * (v < price)
            up, down = up + p * max(v - price, 0.0), down + p * max(price - v, 0.0)
    base, bull = model.scenario("base"), model.scenario("bull")
    expected = model.expected()
    return {"price": price, "expected": expected, "upside": expected / price - 1,
            "base_upside": model.price(base) / price - 1 if base else None,
            "bull_upside": model.price(bull) / price - 1 if bull else None,
            "p_below": below, "ratio": up / down if down > 0 else None}


def reference_stance(stats: dict, band: float) -> tuple[str, str]:
    """按情景数字给出参考立场与理由（与跟踪卡、修正提示词里的定义一致）：
    偏多 = 加权空间 ≥ +band 且跌破现价的概率 < 50%；偏空 = 加权空间 ≤ −band，或基准情景低于现价且跌破现价的概率 > 50%；其余中性。"""
    u, pb, bu = stats["upside"], stats["p_below"], stats["base_upside"]
    if u <= -band:
        return "bearish", f"加权空间 {_pct(u)}，低于 −{band:.0%}"
    if bu is not None and bu < 0 and pb > 0.5:
        return "bearish", f"基准情景低于现价（{_pct(bu)}）、跌破现价的概率 {pb:.0%}"
    if u >= band and pb < 0.5:
        return "bullish", f"加权空间 {_pct(u)}，达到 +{band:.0%}，跌破现价的概率 {pb:.0%}"
    if u >= band:
        return "neutral", f"加权空间 {_pct(u)}，但跌破现价的概率 {pb:.0%}，不足以偏多"
    return "neutral", f"加权空间 {_pct(u)} 在 ±{band:.0%} 以内"


def stance_check(stance: str | None, stats: dict | None, band: float) -> dict | None:
    """报告（或修正）立场与情景数字的对账：{ref, reason, mismatch, note}。立场本身不由代码修改。"""
    if not stats:
        return None
    ref, reason = reference_stance(stats, band)
    note, mismatch = "", bool(stance) and stance != ref
    if mismatch:
        note = (f"报告立场{STANCE_LABEL.get(stance, stance)}，按情景数字的参考立场为{STANCE_LABEL[ref]}（{reason}）")
        if stance == "bearish" and stats["upside"] > 0 and (stats.get("bull_upside") or 0) > 0:
            note += f"；加权空间为正是因为牛市情景（{_pct(stats['bull_upside'])}）抬高了均值"
    elif stance and (stance == "bearish") == (stats["upside"] > 0) and stance != "neutral":
        tail = (f"牛市情景（{_pct(stats['bull_upside'])}）" if stance == "bearish" else "熊市情景")
        note = f"加权空间为{'正' if stats['upside'] > 0 else '负'}，是因为{tail}{'抬高' if stats['upside'] > 0 else '拉低'}了均值；" \
               f"{STANCE_LABEL[stance]}的依据是{reason}"
    return {"ref": ref, "reason": reason, "mismatch": mismatch, "note": note}


def stats_line(stats: dict | None, check: dict | None = None) -> str:
    """一行文字：加权空间、基准情景空间、跌破现价概率、上行/下行比与参考立场。"""
    if not stats:
        return ""
    line = (f"加权空间 {_pct(stats['upside'])}｜基准情景 {_pct(stats['base_upside'])}｜跌破现价概率约 {stats['p_below']:.0%}"
            + (f"｜上行/下行 {stats['ratio']:.2f}" if stats.get("ratio") else ""))
    if check:
        line += f"｜参考立场 {STANCE_LABEL[check['ref']]}" + (" ⚠" if check["mismatch"] else "")
        if check["note"]:
            line += f"（{check['note']}）"
    return line


def _pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:+.2%}" if abs(v) < 0.01 else f"{v:+.1%}"  # 接近 0 时多给一位，避免显示成 -0.0%


def card_stats(card: dict, price: float | None, band: float) -> dict | None:
    """深度报告刚写好时，按跟踪卡的情景区间（中点）与现价计算统计与参考立场，写进跟踪卡（报告附录展示）。"""
    scen = [x for x in card.get("scenarios", []) if x.get("name") in SCENARIOS and x.get("price_low") and x.get("price_high")]
    if not scen or not price:
        return None
    model = _range_model(scen, "", "", "")
    stats = risk_stats(model, price)
    return {**stats, "check": stance_check(card.get("stance"), stats, band)} if stats else None


# ---------------------------------------------------------------- 展示

def fmt(v: float | None, digits: int = 2) -> str:
    return "—" if v is None or not math.isfinite(v) else f"{v:,.{digits}f}"


def describe(model: ValuationModel, target_mode: str = "expected", price: float | None = None, stance: str | None = None,
             band: float = 0.15, anchor_band: float = 0.25) -> str:
    """估值模型的 Markdown 表（提示词、CLI 与报告附录共用）。给出现价时附上情景统计与参考立场。"""
    ccy = model.price_currency
    probs = model.probabilities()
    if model.method == "matrix":
        head = (f"估值方法：{formula(model)}"
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
    out = [head, "", *rows, "", tail]
    if stats := risk_stats(model, price):
        out.append(f"情景统计（现价 {fmt(price)} {ccy}，代码计算）：{stats_line(stats, stance_check(stance, stats, band))}")
    if model.anchor and (base := model.scenario("base")):
        from . import anchor  # anchor → tools、consensus → valuation：放在函数里避免循环导入

        if ac := anchor.check(model.anchor, model.price(base), anchor_band):
            out.append(f"程序参考价（深度报告写作时，代码计算）：{ac['note']}")
    return "\n".join(out)


def formula(model: ValuationModel) -> str:
    """估值公式的一行说明（describe、WebUI 共用）。"""
    if model.basis == "ev":
        return (f"（{model.multiple_label} × {model.driver_label} + 净现金 {fmt(model.net_cash)}）÷ 总股本 "
                f"{fmt(model.shares, 4)} 亿股 × 汇率 {fmt(model.fx, 4)}")
    return f"{model.multiple_label} × {model.driver_label} ÷ 总股本 {fmt(model.shares, 4)} 亿股 × 汇率 {fmt(model.fx, 4)}（股权口径，不加减净现金）"


def _last_close(app, listing) -> float | None:
    bars = app.bars(app.secid(listing), limit=5)
    return float(bars["close"].iloc[-1]) if bars is not None else None


def _ads(listing) -> float | None:
    """每单位股价对应的普通股数：美股 ADS 为换股比例（未填写时未知），其他上市地为 1。"""
    return (listing.ads_ratio or None) if listing.market == "US" else 1.0


def equivalents(app, model: ValuationModel, company, target: float) -> list[str]:
    """其他上市地的等值价格（只读库，不联网）。
    主口径按当前价比折算：目标价 × 该地现价 ÷ 定价上市地现价——各上市地的空间相同，已包含 AH / ADS 溢价与换股比例；
    另给汇率平价（不计溢价）作参考，美股需要在观察清单里填写 ads_ratio。"""
    base = next((l for l in company.listings if l.key == model.listing), None)
    if base is None:
        return []
    p0 = _last_close(app, base)
    out = []
    for l in company.listings:
        if l.key == base.key:
            continue
        p1 = _last_close(app, l)
        line = (f"{l.display} 等值 {fmt(target * p1 / p0)} {l.currency}（按两地现价之比折算，与定价上市地空间相同）"
                if p0 and p1 else f"{l.display}：缺少行情，无法折算")
        r = fx_rate(app, model.price_currency, l.currency)
        if r and _ads(l) and _ads(base):
            line += f"；汇率平价 {fmt(target * r[0] * _ads(l) / _ads(base))} {l.currency}（不计溢价）"
        out.append(line)
    return out


def _last_bar(app, listing) -> tuple[float | None, str | None]:
    bars = app.bars(app.secid(listing), limit=5)
    return (float(bars["close"].iloc[-1]), str(bars["date"].iloc[-1])) if bars is not None else (None, None)


STANCE_RANK = {"bullish": 0, "neutral": 1, "bearish": 2}


def listing_views(app, company, model: ValuationModel, band: float, mode: str = "expected") -> list[dict]:
    """每个上市地各自的视图（只读库，不联网）：现价、目标价、牛 / 基准 / 熊价格与各自的空间、按该地现价算的参考立场。
    定价上市地直接用模型；其他上市地按同股同权的汇率平价折算（ADS 乘换股比例，缺汇率或缺 ads_ratio 时折算不了），
    所以 AH / ADS 溢价会体现为两地不同的空间与立场（例如 H 股折价→偏多，A 股溢价→偏空）。
    统计量（空间、跌破概率、参考立场）用「该地现价折回定价上市地口径」代入 risk_stats，与定价上市地的算法一致。"""
    base = next((l for l in company.listings if l.key == model.listing), None) or company.pricing_listing
    p0, _ = _last_bar(app, base)
    out = []
    for l in company.listings:
        price, day = _last_bar(app, l)
        k: float | None = 1.0
        if l.key != base.key:
            r = fx_rate(app, model.price_currency, l.currency)
            a, b = _ads(l), _ads(base)
            k = r[0] * a / b if r and a and b else None
        v = {"listing": l, "key": l.key, "label": l.display, "currency": l.currency, "price": price, "price_date": day,
             "primary": l.key == base.key, "multi": len(company.listings) > 1, "convertible": k is not None, "target": None, "upside": None, "scenarios": {},
             "stats": None, "stance": None, "reason": "", "premium": None}
        if k is not None and price:
            eq = price / k
            st = risk_stats(model, eq)
            v["stats"] = st
            v["target"] = model.target(mode) * k
            v["upside"] = v["target"] / price - 1
            v["scenarios"] = {s.name: {"price": model.price(s) * k, "upside": model.price(s) * k / price - 1} for s in model.scenarios}
            v["stance"], v["reason"] = reference_stance(st, band)
            v["premium"] = eq / p0 - 1 if p0 and not v["primary"] else None
        out.append(v)
    return out


def view_sort_key(views: list[dict]) -> tuple:
    """观点排序：偏多在前，同观点按空间从大到小，再按上行 / 下行比（多空比）从大到小；没有观点的排最后。取各上市地中最靠前的一个。"""
    keys = [(STANCE_RANK[v["stance"]], -v["upside"], -(v["stats"]["ratio"] if v["stats"]["ratio"] is not None else 1e9)) for v in views if v["stance"] and v["upside"] is not None]
    return min(keys) if keys else (9, 0, 0)
