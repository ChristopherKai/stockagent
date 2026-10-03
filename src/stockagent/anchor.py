"""程序参考价（估值锚点）：一致预期 EPS × 定价上市地自身近 5 年（数据不足时按实际年数）PE(TTM) 分位 × 汇率，全部由代码计算，不调用 LLM。

深度报告的情景价格由模型在敏感性矩阵里挑格点：盈利假设取哪几档、倍数取哪几档都由模型决定，
换一个模型，同样的数据可以得出相差很大的目标价。参考价不依赖模型判断，用在三处：
- 研究与撰写时作为程序计算的材料给模型看；valuation_matrix 自动补上一致预期净利行与历史 PE 分位列（augment）；
- 跟踪卡抽出后，基准情景较参考价偏离超过 tracker.anchor_band 时标注 ⚠（立场与情景价格都不由代码修改）；
- 估值基线沿用写作时的参考价，标的追踪的提示词、公司页与简报里提示偏离。
只覆盖以 A 股或港股定价的公司（美股没有历史 PE 序列）：一致预期来自东方财富（A 股及 A+H，人民币口径），
港股另可用使用者录入或 Nasdaq 快照。
"""

from __future__ import annotations

import re
import statistics
import threading
from datetime import date, timedelta

from requests import RequestException

from . import consensus, valuation
from .app import App, _try
from .config import Company
from .sources import emdata
from .tools import valuation_series
from .utils import log, num, pct, today_local

YEARS = 5         # 历史 PE 分位的回看年数
MIN_DAYS = 120    # PE 序列少于这么多个交易日时不给分位
FX_TOL = 0.03     # 矩阵 fx 与程序汇率相差在此以内，才认为驱动指标与一致预期是同一币种

_cache: dict[tuple[str, str, str], dict | None] = {}
_lock = threading.Lock()


def get(app: App, company: Company) -> dict | None:
    """当天的参考价（同一进程内每家公司每天只算一次；深度报告的研究子任务共用）。"""
    key = (str(app.data_dir), company.id, today_local().isoformat())
    with _lock:
        if key not in _cache:
            try:
                _cache[key] = compute(app, company)
            except Exception as e:  # 参考价只是辅助材料，取不到不能影响深度报告
                log.info("%s：计算程序参考价失败：%s", company.name, e)
                _cache[key] = None
        return _cache[key]


def compute(app: App, company: Company, today: date | None = None) -> dict | None:
    today = today or today_local()
    listing = company.pricing_listing
    if listing.market not in ("A", "HK"):
        return None
    try:
        pe, _, cur, _ = valuation_series(app, listing, YEARS)
    except (RequestException, ValueError, KeyError, TypeError) as e:
        log.info("%s：取历史 PE 失败：%s", company.name, e)
        pe, cur = [], None
    out: dict = {"date": today.isoformat(), "listing": listing.key, "listing_name": listing.display,
                 "currency": listing.currency, "pe": None, "years": []}
    if len(pe) >= MIN_DAYS:
        q = statistics.quantiles(pe, n=4)
        out["pe"] = {"p25": q[0], "p50": q[1], "p75": q[2], "current": cur if cur and cur > 0 else None,
                     "days": len(pe), "years": max(1, min(YEARS, round(len(pe) / 245)))}  # 港股序列受行情与报表期数所限，常不足 5 年
    cache = consensus.ReportCache(app)
    for fy in (today.year, today.year + 1):
        ests = estimates(app, company, fy, today, cache)
        if not ests:
            continue
        y = {"fy": fy, "estimates": ests, "eps": ests[0]["eps"], "fx": ests[0]["fx"]}
        if out["pe"] and y["eps"] > 0 and y["fx"]:
            y.update({k: y["eps"] * y["fx"] * out["pe"][p] for k, p in (("price", "p50"), ("low", "p25"), ("high", "p75"))})
        out["years"].append(y)
    return out if out["pe"] or out["years"] else None


def estimates(app: App, company: Company, fy: int, today: date, cache: consensus.ReportCache) -> list[dict]:
    """fy 财年的一致预期 EPS（可能有多个来源），按机构数从多到少：[{eps, currency, fx, n, source}]。
    fx 为 1 单位 EPS 币种折合定价上市地币种（缺汇率时为 None）。"""
    listing = company.pricing_listing
    after = today + timedelta(days=1)  # 「as_of 之前」的口径：含今天
    out = []
    if (a := company.listing("A")) is not None:
        cur = _try(app, "eastmoney_dc", emdata.a_consensus, app.session, a.code)
        y = next((y for y in (cur or {}).get("years", []) if y["fy"] == fy and not y["actual"] and y["eps"] is not None), None)
        if y:
            out.append({"eps": y["eps"], "currency": "CNY", "n": cur["orgs"] or None,
                        "source": f"东方财富机构预测汇总，{cur['orgs']} 家机构评级"})
        elif cur is None and (snap := consensus._em_snapshot(app, company, fy, after)):
            out.append({"eps": snap["value"], "currency": "CNY", "n": snap["n"], "source": snap["source"]})
        window = app.settings.consensus.window_days
        c = consensus.a_consensus_at(cache.get(a.code, today - timedelta(days=window)), fy, after, window)
        if c:
            out.append({"eps": c["value"], "currency": "CNY", "n": c["n"],
                        "source": f"东方财富研报库中位数，近 {window} 天每家机构最新一篇，{c['n']} 家"})
    else:
        c = consensus.snapshot_consensus_at(app, company, fy, after, listing.currency)
        if c and c["metric"] == "profit":
            shares = valuation.quote_shares(app.db.quote(app.secid(listing)), listing)
            c = {**c, "metric": "eps", "value": c["value"] / (shares * 1e8)} if shares else None
        if c and c["metric"] == "eps":
            out.append({"eps": c["value"], "currency": listing.currency, "n": c.get("n"), "source": c["source"]})
    for e in out:
        r = valuation.fx_rate(app, e["currency"], listing.currency, today)
        e["fx"] = r[0] if r else None
    return sorted(out, key=lambda e: -(e["n"] or 0))


def primary(anc: dict | None) -> dict | None:
    """用来核对基准情景的那一年：明年（12 个月后市场主要按明年盈利定价），没有时用今年。"""
    years = [y for y in (anc or {}).get("years", []) if y.get("price")]
    return max(years, key=lambda y: y["fy"]) if years else None


def _unit(ccy: str) -> str:
    return consensus.CCY_UNIT.get(ccy, ccy)


def _pe_line(anc: dict) -> str:
    pe = anc["pe"]
    return (f"{anc['listing_name']} 近 {pe['years']} 年 PE(TTM) 25%/50%/75% 分位 {pe['p25']:.2f}/{pe['p50']:.2f}/{pe['p75']:.2f} 倍"
            + (f"，当前 {pe['current']:.2f} 倍" if pe.get("current") else "") + f"（{pe['days']} 个交易日）")


def _year_line(anc: dict, y: dict) -> str:
    ests = "；".join(f"{e['eps']:.2f} {_unit(e['currency'])}（{e['source']}）" for e in y["estimates"])
    line = f"{y['fy']} 年一致预期 EPS：{ests}"
    if y.get("price"):
        fx = f" × 汇率 {y['fx']:.4f}" if abs(y["fx"] - 1) > 1e-9 else ""
        line += (f"→ 参考价 {y['eps']:.2f}{fx} × {anc['pe']['p50']:.2f} 倍 = {num(y['price'])} {anc['currency']}"
                 f"（按 25%–75% 分位 {num(y['low'])}–{num(y['high'])}）")
    return line


def text(anc: dict | None, band: float) -> str:
    """给研究 agent 与撰写模型看的材料块（数字全部来自 anc）。"""
    if not anc:
        return ""
    lines = [f"程序参考价（代码计算，{anc['date']}；只作锚点，不是目标价）："]
    if anc["pe"]:
        lines.append(f"- {_pe_line(anc)}")
    lines += [f"- {_year_line(anc, y)}" for y in anc["years"]]
    if not anc["years"]:
        lines.append("- 没有取到一致预期 EPS，只有历史 PE 分位")
    if p := primary(anc):
        lines.append(f"- 敏感性矩阵的驱动指标要覆盖一致预期，倍数要覆盖上述历史分位（工具会自动补上缺的行列）；"
                     f"12 个月情景的基准情景较 {p['fy']} 年参考价 {num(p['price'])} {anc['currency']} 偏离超过 ±{band:.0%} 时，"
                     "必须写明理由：是盈利假设还是倍数与一致预期、历史分位不同，依据是什么")
    return "\n".join(lines)


def check(anc: dict | None, base_price: float | None, band: float) -> dict | None:
    """基准情景价格与参考价的偏离：{ref, base, gap, flag, fy, note}；没有参考价时返回 None。"""
    y = primary(anc)
    if not y or not base_price or base_price <= 0:
        return None
    gap = base_price / y["price"] - 1
    flag = abs(gap) > band
    fx = f" × 汇率 {y['fx']:.4f}" if abs(y["fx"] - 1) > 1e-9 else ""
    note = (f"基准情景 {num(base_price)} 较参考价 {num(y['price'])} {anc['currency']} {pct(gap)}；参考价 = {y['fy']} 年一致预期 EPS "
            f"{y['eps']:.2f} {_unit(y['estimates'][0]['currency'])}（{y['estimates'][0]['source']}）{fx} × {anc['listing_name']} "
            f"近 {anc['pe']['years']} 年 PE(TTM) 中位数 {anc['pe']['p50']:.2f} 倍（{anc['date']}）")
    if flag:
        note += f"；偏离超过 ±{band:.0%}，请核对报告是否写明盈利假设或倍数与一致预期、历史估值不同的理由"
    return {"ref": y["price"], "base": base_price, "gap": gap, "flag": flag, "fy": y["fy"], "note": note}


_PE = re.compile(r"P\s*/?\s*E(?!G)", re.IGNORECASE)
_PROFIT = re.compile(r"净利|利润")
_NOT_PROFIT = re.compile(r"EPS|每股|收入|营收|EBIT|现金流", re.IGNORECASE)


def augment(anc: dict, driver_label: str, drivers: list[float], multiple_label: str, multiples: list[float],
            shares: float, fx: float, basis: str) -> tuple[list[float], list[float], list[str]]:
    """估值矩阵自动补行列：股权口径的 PE 矩阵补上定价上市地近 5 年 PE 分位；驱动指标是某年净利润时补上
    一致预期净利（EPS × 股本，与矩阵同一数量级）。返回 (驱动取值, 倍数, 说明)。"""
    notes = []
    pe = anc.get("pe")
    if pe and basis == "equity" and _PE.search(multiple_label or "") and "EV" not in (multiple_label or "").upper():
        add = sorted({round(pe[k], 1) for k in ("p25", "p50", "p75")
                      if pe[k] > 0 and all(abs(pe[k] / m - 1) > 0.03 for m in multiples if m > 0)})
        if add:
            multiples = sorted(set(multiples) | set(add))
            notes.append(f"程序已补上倍数列 {'、'.join(f'{m:g}×' for m in add)}（{_pe_line(anc)}）")
    fy = valuation._year(driver_label or "")
    y = next((y for y in anc.get("years", []) if y["fy"] == fy), None)
    if y and _PROFIT.search(driver_label) and not _NOT_PROFIT.search(driver_label) and drivers:
        lo, hi = min(drivers), max(drivers)
        added = []
        for e in y["estimates"]:
            if not e["fx"] or abs(fx / e["fx"] - 1) > FX_TOL or e["eps"] <= 0:
                continue  # 矩阵的驱动指标不是一致预期的币种（看 fx 是否等于该币种折合股价币种的汇率）
            v = float(f"{e['eps'] * shares:.4g}")
            if lo / 3 <= v <= hi * 3 and all(abs(v / d - 1) > 0.01 for d in drivers + added if d):
                added.append(v)
                notes.append(f"程序已补上驱动行 {v:g}（{fy} 年一致预期 EPS {e['eps']:.2f} {_unit(e['currency'])} × 股本 {shares:g}，{e['source']}）")
        drivers = sorted(set(drivers) | set(added))
    if p := primary(anc):
        notes.append(f"程序参考价：{_year_line(anc, p)}")
    return drivers, multiples, notes
