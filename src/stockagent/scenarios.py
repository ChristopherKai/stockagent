"""程序定价的 12 个月情景：模型只在估值工具的结果里「选点」，价格由代码算出，不再从报告文字里反推。

此前的链路是：研究 agent 调 valuation_matrix → 写手把情景价格写进正文 → 抽卡模型再从正文抽出 price_low/high →
追踪时 valuation.build_baseline 再去矩阵里反找能解释这些价格的格点。任何一环措辞漂移（四舍五入、写错一个数），
基线就退化成只能按比例修正的区间模型。
现在：撰写前让模型从工具日志里选一个估值，为牛/基/熊定出（驱动指标, 倍数区间, 概率），价格由代码按公式计算。
备选估值有两种：
- 敏感性矩阵（valuation_matrix）：价格 = 矩阵公式（tools.matrix_price），驱动与倍数必须落在矩阵取值范围内；
- 分部加总（sotp，parts 带 driver 与 multiple，或只有 value 时视为 驱动 = 价值、倍数 = 1）：
  价格 =（Σ 分部驱动 × 倍数 × 持股 ×（1 − 折价）+ 净现金）×（1 − 控股折价）÷ 股本 × 汇率（tools.sotp_price）。
  分部名称、持股、股本、净现金、汇率相同的多次 sotp 调用算作同一个分部加总，它们逐分部的取值合起来就是该分部
  驱动、倍数与折价的取值范围，控股公司折价同理（研究时对牛/基/熊各算一次；折价与控股折价通常随情景变）。
结果作为「程序计算」笔记交给写手照抄，跟踪卡的情景直接取这里的数，并把参数一起存进跟踪卡（card["valuation_spec"]），
build_baseline 优先用它精确重建。没有可用的估值（或模型选不出合格的点）时返回 None，沿用原来的流程。
"""

from __future__ import annotations

import logging
import math
import re

from . import valuation
from .llm import LLMError

log = logging.getLogger(__name__)

GRID_MARGIN = 0.10   # 驱动指标与倍数可以超出工具结果取值范围的比例（研究本应覆盖情景）
DISCOUNT_MARGIN = 0.05  # 折价、控股折价可以超出工具结果取值范围的绝对量（折价常为 0，不能按比例放宽）
BASIS_LABEL = {"equity": "股权口径（PE、PB 等）", "ev": "EV 口径（加净现金）"}
NAMES = ("bull", "base", "bear")
DIVERGENCE = 0.30      # 矩阵与 SOTP 的基准价相差超过这个比例：不自动采用 SOTP，退回矩阵并标出分歧
SCEN_LABEL = {"bull": "牛市", "base": "基准", "bear": "熊市"}

SYSTEM = """你是估值研究员。研究笔记里有若干次估值工具调用：敏感性矩阵（valuation_matrix）或分部加总（sotp）。请从备选估值中选一个，为牛市 / 基准 / 熊市三个情景各定出一个点，只输出 JSON。选哪一个：以研究笔记里明确指定为情景定价「主锚」的估值方法为准（笔记写明以敏感性矩阵为主锚，就在矩阵里选；写明以分部加总为主锚，就在分部加总里选；同类有多个时选与笔记描述最吻合的）；笔记没有明确指定时选敏感性矩阵。不要因为某种方法算出的价格更高或更低而改选。价格由程序按公式计算，你不要自己算价格。
- matrix：所选估值的编号（备选清单方括号里的数字）。
- 每个情景：probability 是 0–1 的小数，三个情景合计为 1；drivers 用一两句话写清这个情景的关键驱动，要有研究笔记的依据。
- 选的是矩阵时：driver 是驱动指标的取值（单位与矩阵一致），multiple_low、multiple_high 是倍数区间，parts 写空数组。
- 选的是分部加总时：parts 为每个分部各一项（name 与清单里的分部名一字不差），写这个情景下该分部的 driver（驱动指标，可以为负，如总部费用）、multiple_low、multiple_high（倍数区间）和 discount（该分部折价 0–1）；情景层面再写 holdco_discount（控股公司折价 0–1）；情景层面的 driver、multiple_low、multiple_high 都写 0。只给了价值（倍数固定为 1）的分部，multiple_low、multiple_high 都写 1。
- driver、倍数、折价必须落在所选估值清单列出的取值范围之内；基准情景的驱动要贴近一致预期（笔记里有「程序参考价」时对照它），倍数区间要有依据（定价上市地自身的历史分位、可比公司）。
- 牛市价格 ≥ 基准 ≥ 熊市；概率要反映证据，不要机械地写 25/50/25。"""


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


SCHEMA = _obj({
    "matrix": {"type": "integer"},
    "scenarios": {"type": "array", "items": _obj({
        "name": {"type": "string", "enum": list(NAMES)}, "driver": {"type": "number"},
        "multiple_low": {"type": "number"}, "multiple_high": {"type": "number"},
        "probability": {"type": "number"}, "drivers": {"type": "string"}, "holdco_discount": {"type": "number"},
        "parts": {"type": "array", "items": _obj({
            "name": {"type": "string"}, "driver": {"type": "number"},
            "multiple_low": {"type": "number"}, "multiple_high": {"type": "number"}, "discount": {"type": "number"}})}})},
})


# ---------------------------------------------------------------- 备选估值

def matrix_menu(app, company, tool_log: list[dict]) -> list[dict]:
    """工具日志里可用的估值矩阵（每次调用一条；口径明确的取记录值，旧调用取按倍数名称推断的口径）。"""
    listing = company.pricing_listing
    shares_ref = valuation.quote_shares(app.db.quote(app.secid(listing)), listing)
    cands, _ = valuation.matrix_candidates(tool_log, shares_ref)
    last: dict[int, dict] = {}
    for c in cands:  # 同一次调用可能有多个备选口径，推断的口径排在后面
        last[c["index"]] = {**c, "kind": "matrix"}
    return [last[i] for i in sorted(last)]


def _part_point(p: dict) -> tuple[float, float] | None:
    """sotp 一个分部的（驱动, 倍数）：给了 driver 与 multiple 就用它们，只有 value 时 驱动 = 价值、倍数 = 1。"""
    try:
        if p.get("driver") is not None and p.get("multiple") is not None:
            return float(p["driver"]), float(p["multiple"])
        return float(p["value"]), 1.0
    except (KeyError, TypeError, ValueError):
        return None


def sotp_menu(app, company, tool_log: list[dict]) -> list[dict]:
    """工具日志里的分部加总。分部名称、持股、折价、股本、净现金、汇率、控股折价都相同的多次调用合并为一项，
    逐分部的驱动与倍数取值合起来作为可选范围。"""
    listing = company.pricing_listing
    shares_ref = valuation.quote_shares(app.db.quote(app.secid(listing)), listing)
    groups: dict[tuple, dict] = {}
    for i, x in enumerate(tool_log):
        if x.get("tool") != "sotp" or str(x.get("result", "")).startswith("错误"):
            continue
        a = x.get("args") or {}
        try:
            shares, net_cash, fx, holdco = float(a["shares"]), float(a.get("net_cash") or 0), float(a.get("fx") or 1), float(a.get("holdco_discount") or 0)
            parts = [{"name": str(p["name"]), "stake": float(p.get("stake", 1.0)), "discount": float(p.get("discount", 0.0)),
                      "method": str(p.get("method", "")), "point": _part_point(p)} for p in a["parts"]]
        except (KeyError, TypeError, ValueError):
            continue
        names = [p["name"] for p in parts]
        if shares <= 0 or not parts or len(set(names)) != len(names) or any(p["point"] is None for p in parts):
            continue
        if shares_ref and abs(shares / shares_ref - 1) > valuation.SHARES_TOL:
            continue
        key = (tuple(names), tuple(p["stake"] for p in parts), shares, net_cash, fx)  # 折价与控股折价不入键：它们本来就随情景变
        g = groups.setdefault(key, {"kind": "sotp", "calls": [], "shares": shares, "net_cash": net_cash, "fx": fx, "holdcos": [],
                                    "segments": [{"name": p["name"], "stake": p["stake"], "method": p["method"],
                                                  "drivers": [], "multiples": [], "discounts": []} for p in parts]})
        g["calls"].append(i)
        g["index"] = i  # 编号取最后一次调用（编号只用来选择，且各工具调用的位置互不相同）
        g["holdcos"].append(holdco)
        for seg, p in zip(g["segments"], parts):
            seg["drivers"].append(p["point"][0])
            seg["multiples"].append(p["point"][1])
            seg["discounts"].append(p["discount"])
            seg["method"] = seg["method"] or p["method"]
    return sorted(groups.values(), key=lambda g: g["index"])


def build_menu(app, company, tool_log: list[dict]) -> list[dict]:
    return sorted(matrix_menu(app, company, tool_log) + sotp_menu(app, company, tool_log), key=lambda c: c["index"])


def _calls(calls: list[int]) -> str:
    return "、".join(f"#{i + 1}" for i in calls)


def menu_text(menu: list[dict]) -> str:
    out = []
    for c in menu:
        if c["kind"] == "sotp":
            def vals(xs, f="g"):
                return ", ".join(format(x, f) for x in sorted(set(xs)))
            segs = "；".join(f"{g['name']}（持股 {g['stake']:.0%}；驱动指标取值 {vals(g['drivers'])}；倍数取值 {vals(g['multiples'])}；折价取值 {vals(g['discounts'], '.0%')}）"
                              for g in c["segments"])
            out.append(f"[{c['index']}] 分部加总 SOTP（工具调用 {_calls(c['calls'])}）：股本 {c['shares']:g} 亿股；净现金 {c['net_cash']:g}；"
                       f"汇率 {c['fx']:g}；控股公司折价取值 {vals(c['holdcos'], '.0%')}；分部：{segs}")
            continue
        a = c["args"]
        drivers, mults = [float(x) for x in a["driver_values"]], [float(x) for x in a["multiples"]]
        out.append(f"[{c['index']}] 敏感性矩阵：驱动指标 {a.get('driver_label', '')}：{', '.join(f'{d:g}' for d in drivers)}；"
                   f"倍数 {a.get('multiple_label', '')}：{', '.join(f'{m:g}' for m in mults)}；{BASIS_LABEL[c['basis']]}；"
                   f"股本 {float(a['shares']):g} 亿股；净现金 {float(a.get('net_cash') or 0):g}；汇率 {float(a.get('fx') or 1):g}")
    return "\n".join(out)


# ---------------------------------------------------------------- 校验与定价

def _within(v: float, grid: list[float]) -> bool:
    lo, hi = min(grid), max(grid)
    return lo - abs(lo) * GRID_MARGIN <= v <= hi + abs(hi) * GRID_MARGIN


def _point(who: str, d: float, lo: float, hi: float, drivers: list[float], mults: list[float], negative_ok: bool = False) -> None:
    """驱动与倍数必须落在工具结果的取值范围内。SOTP 的分部驱动可以为负（总部费用等），倍数始终为正。"""
    if not all(math.isfinite(x) for x in (d, lo, hi)) or (d == 0 if negative_ok else d <= 0) or lo <= 0 or hi < lo:
        raise ValueError(f"{who}的数值不合理：driver={d}，倍数 {lo}–{hi}")
    if not _within(d, drivers) or not _within(lo, mults) or not _within(hi, mults):
        raise ValueError(f"{who}超出所选估值的取值范围：driver={d:g}（范围 {min(drivers):g}–{max(drivers):g}），"
                         f"倍数 {lo:g}–{hi:g}（范围 {min(mults):g}–{max(mults):g}）")


def _discount(who: str, v, grid: list[float]) -> float:
    v = float(v)
    if not math.isfinite(v) or not 0 <= v < 1:
        raise ValueError(f"{who}的折价 {v} 应在 [0, 1) 内")
    if not min(grid) - DISCOUNT_MARGIN <= v <= max(grid) + DISCOUNT_MARGIN:
        raise ValueError(f"{who}的折价 {v:.0%} 超出所选估值的取值范围 {min(grid):.0%}–{max(grid):.0%}")
    return v


def _spec_matrix(entry: dict, pts: list[dict]) -> dict:
    a, basis = entry["args"], entry["basis"]
    drivers, mults = [float(x) for x in a["driver_values"]], [float(x) for x in a["multiples"]]
    points = []
    for p in pts:
        d, lo, hi = float(p["driver"]), float(p["multiple_low"]), float(p["multiple_high"])
        _point(f"{p['name']} 情景", d, lo, hi, drivers, mults)
        points.append({"name": p["name"], "driver": d, "multiple_low": lo, "multiple_high": hi})
    return {"kind": "matrix", "call": entry["index"], "basis": basis, "driver_label": a.get("driver_label", "驱动指标"),
            "multiple_label": a.get("multiple_label", "倍数"), "shares": float(a["shares"]),
            "net_cash": float(a.get("net_cash") or 0), "fx": float(a.get("fx") or 1), "points": points}


def _spec_sotp(entry: dict, pts: list[dict]) -> dict:
    segs = {g["name"]: g for g in entry["segments"]}
    points = []
    for p in pts:
        parts = p.get("parts") or []
        if sorted(x.get("name") for x in parts) != sorted(segs):
            raise ValueError(f"{p['name']} 情景的分部要与清单一致（{list(segs)}），实际是 {[x.get('name') for x in parts]}")
        out = []
        for name in segs:  # 按清单的分部顺序
            x = next(x for x in parts if x["name"] == name)
            who = f"{p['name']} 情景的分部「{name}」"
            d, lo, hi = float(x["driver"]), float(x["multiple_low"]), float(x["multiple_high"])
            _point(who, d, lo, hi, segs[name]["drivers"], segs[name]["multiples"], negative_ok=True)
            out.append({"name": name, "driver": d, "multiple_low": lo, "multiple_high": hi,
                        "discount": _discount(who, x.get("discount", 0.0), segs[name]["discounts"])})
        points.append({"name": p["name"], "parts": out,
                       "holdco_discount": _discount(f"{p['name']} 情景的控股公司", p.get("holdco_discount", 0.0), entry["holdcos"])})
    base = next(p for p in points if p["name"] == "base")
    return {"kind": "sotp", "call": entry["index"], "calls": entry["calls"], "shares": entry["shares"], "net_cash": entry["net_cash"],
            "fx": entry["fx"], "holdco_discount": base["holdco_discount"], "points": points,
            "segments": [{k: g[k] for k in ("name", "stake", "method")} for g in entry["segments"]],
            "driver_label": "分部价值", "multiple_label": "分部倍数"}


def build_spec(menu: list[dict], data: dict) -> dict:
    """校验模型的选择并整理成 spec（含所选估值的全部参数）。不合格抛 ValueError，LLM 层会带着原因重试一次。"""
    entry = next((c for c in menu if c["index"] == data.get("matrix")), None)
    if entry is None:
        raise ValueError(f"matrix={data.get('matrix')} 不在备选清单里（可选：{[c['index'] for c in menu]}）")
    pts = sorted(data.get("scenarios") or [], key=lambda x: NAMES.index(x["name"]) if x.get("name") in NAMES else 9)
    names = [p.get("name") for p in pts]
    if names != list(NAMES):
        raise ValueError(f"需要 bull / base / bear 各一个情景，实际是 {names}")
    spec = (_spec_sotp if entry["kind"] == "sotp" else _spec_matrix)(entry, pts)
    probs = [float(p["probability"]) for p in pts]
    if any(not math.isfinite(x) or x < 0 for x in probs) or sum(probs) <= 0:
        raise ValueError(f"情景概率不合理：{probs}")
    for point, prob, p in zip(spec["points"], probs, pts):
        point["probability"] = prob / sum(probs)
        point["drivers"] = str(p.get("drivers", "")).strip()
    mid = {s["name"]: (s["price_low"] + s["price_high"]) / 2 for s in compute(spec)}
    if not mid["bull"] >= mid["base"] >= mid["bear"] or mid["base"] <= 0:
        raise ValueError(f"情景价格顺序不对或基准价格不为正：牛 {mid['bull']:.2f}、基 {mid['base']:.2f}、熊 {mid['bear']:.2f}"
                         "（需要 牛 ≥ 基准 ≥ 熊，且基准 > 0）")
    return spec


def compute(spec: dict) -> list[dict]:
    return valuation.spec_scenarios(spec)


def describe_spec(spec: dict) -> str:
    return "分部加总（SOTP）" if spec.get("kind") == "sotp" else f"{spec['driver_label']} × {spec['multiple_label']}"


def table_text(spec: dict, price: float | None, currency: str) -> str:
    """交给写手的「程序计算」情景表（写进研究笔记，也作为数字溯源的语料）。"""
    scen = compute(spec)
    rows = {p["name"]: p for p in spec["points"]}
    if spec.get("kind") == "sotp":
        how = (f"工具调用 {_calls(spec['calls'])} 的分部加总：股本 {spec['shares']:g} 亿股，净现金 {spec['net_cash']:g}，"
               f"汇率 {spec['fx']:g}；价格 =（Σ 分部驱动 × 倍数 × 持股 ×（1 − 折价）+ 净现金）×（1 − 控股公司折价）÷ 股本 × 汇率")
        cols = "分部（驱动指标 × 倍数区间）"
    else:
        how = (f"工具调用 #{spec['call'] + 1} 的矩阵：{spec['driver_label']} × {spec['multiple_label']}，{BASIS_LABEL[spec['basis']]}，"
               f"股本 {spec['shares']:g} 亿股，净现金 {spec['net_cash']:g}，汇率 {spec['fx']:g}")
        cols = f"{spec['driver_label']} × {spec['multiple_label']}区间"
    lines = [f"以下 12 个月情景的价格由程序按估值公式计算（{how}）。报告的「12 个月情景」章节必须照抄这里的价格区间与概率，不得改动；分析与理由可以自己写。",
             "", f"| 情景 | 概率 | {cols} | 价格区间（{currency}） | 相对现价 | 关键驱动 |", "|---|---|---|---|---|---|"]
    for s in scen:
        r = rows[s["name"]]
        mid = (s["price_low"] + s["price_high"]) / 2
        rel = f"{mid / price - 1:+.1%}" if price else "—"
        if spec.get("kind") == "sotp":
            stake = {g["name"]: g["stake"] for g in spec["segments"]}
            detail = "；".join(f"{x['name']} {x['driver']:g} × {x['multiple_low']:g}–{x['multiple_high']:g}×"
                              + (f"（持股 {stake[x['name']]:.0%}、折价 {x['discount']:.0%}）" if stake[x["name"]] < 1 or x["discount"] else "")
                              for x in r["parts"]) + f"；控股公司折价 {r['holdco_discount']:.0%}"
        else:
            detail = f"{r['driver']:g} × {r['multiple_low']:g}–{r['multiple_high']:g}×"
        lines.append(f"| {SCEN_LABEL[s['name']]} | {s['probability']:.0%} | {detail} | "
                     f"{s['price_low']:.2f}–{s['price_high']:.2f} | {rel} | {s['drivers']} |")
    expected = sum(s["probability"] * (s["price_low"] + s["price_high"]) / 2 for s in scen)
    lines += ["", f"概率加权价格（区间中点，程序计算）：{expected:.2f} {currency}"
              + (f"，相对现价 {price:.2f} 为 {expected / price - 1:+.1%}" if price else "")]
    if note := cross_check_text(spec):
        lines += ["", note]
    return "\n".join(lines)


CROSS_SYSTEM = """这是交叉检查：只能从下面的备选里选，不用考虑哪种方法被指定为主锚，也不要设法让价格与另一种方法接近。照实选出你认为最合理的点。"""


def _pick(app, company, menu: list[dict], notes_text: str, market: str, currency: str, extra: str = "") -> dict:
    """让模型在 menu 里选点并校验，返回 spec；模型调用或校验失败抛 LLMError。"""
    listing = company.pricing_listing
    user = (f"公司：{company.title}\n定价上市地：{listing.display}（{currency}）——估值的汇率应把报告币种折成 {currency}。\n"
            f"当前行情：\n{market}\n{app.fx_line()}\n\n备选估值：\n{menu_text(menu)}\n\n研究笔记：\n{notes_text}")
    return app.llm.complete("dossier", [SYSTEM, extra] if extra else [SYSTEM], user, schema=SCHEMA,
                            validate=lambda d: build_spec(menu, d),
                            max_tokens=48000).data  # 推理型模型选点前会长篇推演，16000 实测会在 JSON 之前就用完


KIND_LABEL = {"matrix": "敏感性矩阵", "sotp": "分部加总（SOTP）"}


def base_price(spec: dict) -> float:
    """基准情景的价格（区间中点）。"""
    s = next(x for x in compute(spec) if x["name"] == "base")
    return (s["price_low"] + s["price_high"]) / 2


def propose(app, company, notes_text: str, tool_log: list[dict], market: str, currency: str) -> tuple[dict | None, str]:
    """让模型在备选估值里选点，返回 (spec, 说明)。没有可用估值、模型调用失败或两次都选不出合格的点时 spec 为 None。
    矩阵与分部加总两种都有时做交叉检查：另一种方法也选一次点，基准价相差超过 DIVERGENCE 就不自动采用 SOTP——
    主锚是 SOTP 时退回矩阵，主锚是矩阵时保持矩阵——并把分歧记在 spec["cross_check"] 里（写进研究笔记与跟踪卡）。"""
    menu = build_menu(app, company, tool_log)
    if not menu:
        return None, "研究过程中没有可用的估值矩阵或分部加总，情景价格按报告正文抽取"
    try:
        spec = _pick(app, company, menu, notes_text, market, currency)
    except LLMError as e:
        return None, f"程序定价情景失败（{str(e)[:200]}），情景价格按报告正文抽取"
    other = "matrix" if spec["kind"] == "sotp" else "sotp"
    others = [c for c in menu if c["kind"] == other]
    if not others:
        return spec, ""
    try:
        alt = _pick(app, company, others, notes_text, market, currency, CROSS_SYSTEM)
    except LLMError as e:
        spec["cross_check"] = {"status": "unverified", "other_kind": other, "why": str(e)[:200]}
        return spec, f"另一种估值（{KIND_LABEL[other]}）选点失败，未能交叉检查：{str(e)[:120]}"
    mp, ms = (base_price(spec), base_price(alt)) if spec["kind"] == "matrix" else (base_price(alt), base_price(spec))  # 矩阵、SOTP 的基准价
    gap = ms / mp - 1 if mp else float("inf")  # 以矩阵为参照
    info = {"status": "ok", "matrix_base": mp, "sotp_base": ms, "gap": gap, "threshold": DIVERGENCE, "primary": spec["kind"], "switched": False}
    if abs(gap) > DIVERGENCE:
        info["status"] = "diverged"
        if spec["kind"] == "sotp":  # 不自动选 SOTP：退回矩阵
            spec, info["switched"] = alt, True
    spec["cross_check"] = info
    if info["status"] == "diverged":
        return spec, (f"矩阵基准价 {mp:.2f} 与 SOTP 基准价 {ms:.2f} 相差 {gap:+.0%}，超过 {DIVERGENCE:.0%}："
                      + ("不自动采用 SOTP，退回矩阵" if info["switched"] else "仍以矩阵为主锚") + "，分歧已标在跟踪卡里")
    return spec, ""


def cross_check_text(spec: dict) -> str:
    """交叉检查结果的一句话（研究笔记、跟踪卡共用）；没有做交叉检查返回空字符串。"""
    cc = spec.get("cross_check")
    if not cc:
        return ""
    if cc["status"] == "unverified":
        return f"交叉检查未完成：另一种估值（{KIND_LABEL[cc['other_kind']]}）没有选出合格的点（{cc.get('why', '')}），无法核对。"
    head = (f"交叉检查：矩阵基准价 {cc['matrix_base']:.2f}、SOTP 基准价 {cc['sotp_base']:.2f}，SOTP 相对矩阵 {cc['gap']:+.0%}。")
    if cc["status"] == "ok":
        return head + f"两种方法的差异在 {cc['threshold']:.0%} 以内。"
    return (head + f"相差超过 {cc['threshold']:.0%}，" + ("不自动采用 SOTP，已退回矩阵。" if cc["switched"] else "仍以矩阵为主锚。")
            + "两种方法为什么差这么多（分部口径、少数股东、折价、倍数依据）需要在报告中交代。")


SECTION_TITLE = "12 个月情景"


def report_mismatches(report: str, spec: dict) -> list[str]:
    """报告「12 个月情景」章节里找不到的程序价格端点（容差 0.6%）。写手被要求照抄，这里只做核对、不改报告；找不到章节时不判断。"""
    lines = report.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("## ") and SECTION_TITLE in l), None)
    if start is None:
        return []
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    nums = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", "\n".join(lines[start:end]))]
    out = []
    for s in compute(spec):
        for end_name, v in (("下沿", s["price_low"]), ("上沿", s["price_high"])):
            if not any(abs(n - v) <= max(0.006 * v, 0.006) for n in nums):
                out.append(f"{SCEN_LABEL[s['name']]}{end_name} {v:.2f}")
    return out
