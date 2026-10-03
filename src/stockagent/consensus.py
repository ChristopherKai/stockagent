"""业绩 vs 一致预期：实际值（定期报告、业绩预告、业绩快报）与发布前的一致预期对比，全部由代码计算，不调用 LLM。

一致预期从哪里来：
- A 股（含 A+H 公司，用 A 股口径、人民币）：东方财富研报库按发布日还原「当时」的一致预期——发布前 window_days 天内
  每家机构取最新一篇研报，EPS 取中位数。研报库带发布日，所以历史各期都能回溯。
- 美股：Nasdaq 年度 EPS 一致预期只有当前值，每天存一份快照（consensus 表），比较时取发布前最近一份；
  中概股 ADS 口径按 ads_ratio 换成每股、按发布日汇率换成报告币种。
- 港股：没有免费的盈利预测，由使用者录入（WebUI 公司页或 stock consensus set），同样存快照、取发布前最近一份。

怎么比：年报直接比（偏差 = 实际 ÷ 一致预期 − 1）；中报、季报是年初至今累计，看完成全年一致预期的比例，
并用近 3 年同期占比外推全年——只在历史占比稳定（至少 2 年可比、最高与最低相差不超过 NORM_SPREAD）时外推；
周期股等占比忽高忽低时只给完成率和历史区间，不给外推的偏差百分比（模型会把它当成硬证据）。
一致预期为亏损或接近零时不算百分比，只给差额。

结果存 earnings 表，并生成一条「一致预期对比」数据条目（摘要以「[程序计算]」开头）进入分级，
之后与新闻公告一样流向证伪条件检查、标的追踪与简报。
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import date, datetime, time, timedelta

from . import valuation
from .app import App, _item, _try
from .config import Company, Listing
from .sources import emdata, nasdaq
from .utils import CST, iso, log, parse_iso, pct, to_simplified, today_local, utcnow

SOURCE = "consensus"
TAG = "[程序计算]"
STAGE_LABEL = {"forecast": "业绩预告", "express": "业绩快报", "report": "定期报告"}
PERIOD_LABEL = {3: "一季报", 6: "中报", 9: "三季报", 12: "年报"}
METRIC_LABEL = {"eps": "EPS", "profit": "归母净利", "revenue": "营业收入"}
SNAPSHOT_LABEL = {"em_a": "东方财富机构预测", "nasdaq": "Nasdaq", "manual": "使用者录入"}
CCY_UNIT = {"CNY": "元", "HKD": "港元", "USD": "美元"}
PENDING_DAYS = 30   # 港股业绩已发布但缺少发布前一致预期时，最多等使用者补录多少天
NORM_SPREAD = 0.2   # 历史同期占比的最高与最低相差超过它（或只有 1 年可比）时，不按季节性外推全年
# 与业绩对应的公告（标题 + 类别，先转简体再匹配）。港股的业绩以「[中期業績] / [末期業績] / [季度業績]」类别的公告发布，
# 一个月后才刊发的「中期報告 / 年報」全文不是业绩发布；A 股的定期报告本身就是业绩发布
NOTICE_TITLES = {"forecast": r"业绩预告|盈利预告|盈利警告", "express": r"业绩快报",
                 "report": {"A": r"年度报告|半年度报告|季度报告", "HK": r"中期业绩|末期业绩|全年业绩|年度业绩|季度业绩|业绩公告"}}
NOT_RESULTS = re.compile(r"说明会|问询|更正|补充公告|海外监管公告")  # 业绩说明会通知、问询函回复、A+H 公司的境外监管公告镜像


# ---------------------------------------------------------------- 展示

def show(metric: str, v: float | None, ccy: str) -> str:
    if v is None:
        return "—"
    unit = CCY_UNIT.get(ccy, ccy)
    return f"{v:,.2f} {unit}" if metric == "eps" else f"{v / 1e8:,.2f} 亿{unit}"


def period_info(period_end: str) -> tuple[int, int]:
    d = date.fromisoformat(period_end[:10])
    return d.year, d.month


def period_label(period_end: str) -> str:
    fy, month = period_info(period_end)
    return f"{fy} {PERIOD_LABEL.get(month, f'{month} 月')}"


# ---------------------------------------------------------------- 实际值

def actual_rows(rows: list[dict], market: str) -> list[dict]:
    """emdata.financials 的结果 → [{period_end, notice, eps, profit, revenue, currency}]（年初至今累计，按报告期倒序）。"""
    out = []
    for r in rows:
        if not r.get("_report_date"):
            continue
        out.append({"period_end": r["_report_date"], "notice": (r.get("公告日") or "")[:10] or None,
                    "eps": r.get("EPS"), "profit": r.get("归母净利"),
                    "revenue": r.get("营业收入") if market == "A" else r.get("营业额"),
                    "currency": r.get("币种") or "CNY"})
    return out


def seasonal_norm(history: list[dict], fy: int, month: int, metric: str, years: int = 3) -> dict | None:
    """近 years 年「同期累计 ÷ 全年」的占比（只用两者都为正的年份）。"""
    by = {period_info(r["period_end"]): r for r in history}
    ratios = []
    for y in range(fy - 1, fy - 1 - years, -1):
        part, full = (by.get((y, month)) or {}).get(metric), (by.get((y, 12)) or {}).get(metric)
        if part is not None and full and full > 0 and part > 0:
            ratios.append((y, part / full))
    if not ratios:
        return None
    vals = [r for _, r in ratios]
    return {"mean": statistics.mean(vals), "min": min(vals), "max": max(vals), "years": [y for y, _ in ratios]}


def shares_estimate(history: list[dict]) -> float | None:
    """股本（股）≈ 归母净利 ÷ EPS，取最近一期两者都为正的报告期；用于把业绩预告的归母净利换算成 EPS。"""
    for r in history:
        if r.get("profit") and r.get("eps") and r["profit"] > 0 and r["eps"] > 0:
            return r["profit"] / r["eps"]
    return None


# ---------------------------------------------------------------- 一致预期

class ReportCache:
    """一次检查内复用的 A 股研报列表（每只股票只按最宽的时间窗抓一次）。"""

    def __init__(self, app: App):
        self.app = app
        self.rows: dict[str, tuple[date, list[dict]]] = {}

    def get(self, a_code: str, since: date) -> list[dict]:
        have = self.rows.get(a_code)
        if have and have[0] <= since:
            return have[1]
        rows = _try(self.app, "eastmoney_report", emdata.research_reports, self.app.session, a_code,
                    begin=since, end=today_local(), size=100, max_pages=10)
        if rows is None:
            return have[1] if have else []
        self.rows[a_code] = (since, rows)
        return rows


def a_consensus_at(reports: list[dict], fy: int, as_of: date, window_days: int) -> dict | None:
    """as_of 之前 window_days 天内发布的研报，每家机构取最新一篇，对 fy 财年 EPS 取中位数。"""
    begin, end = (as_of - timedelta(days=window_days)).isoformat(), as_of.isoformat()
    latest: dict[str, tuple[str, float]] = {}
    for r in sorted(reports, key=lambda r: r["date"]):
        v = (r.get("years") or {}).get(fy)
        if v is None or not r.get("org") or not (begin <= r["date"] < end):
            continue
        latest[r["org"]] = (r["date"], v)
    if not latest:
        return None
    vals = [v for _, v in latest.values()]
    return {"fy": fy, "metric": "eps", "value": statistics.median(vals), "mean": statistics.mean(vals), "low": min(vals),
            "high": max(vals), "n": len(vals), "as_of": max(d for d, _ in latest.values()), "currency": "CNY",
            "source": f"东方财富研报库（{begin} 至 {(as_of - timedelta(days=1)).isoformat()} 发布，每家机构取最新一篇的中位数）",
            "flags": []}


def snapshot_consensus_at(app: App, company: Company, fy: int, as_of: date, report_ccy: str) -> dict | None:
    """发布前最近一份快照：使用者录入优先（EPS、归母净利、营收依次），其次 Nasdaq（ADS 口径换成每股）。"""
    before = (as_of - timedelta(days=1)).isoformat()
    for metric in ("eps", "profit", "revenue"):
        rows = app.db.consensus_rows(company.id, fiscal_year=fy, metric=metric, on_or_before=before, sources=("manual",))
        if rows:
            r = rows[0]
            return _converted(app, r, r["value"], report_ccy, as_of,
                              f"使用者录入（{r['note'] or '未注明来源'}，数据日期 {r['taken_on']}）", [])
    us = company.listing("US")
    rows = app.db.consensus_rows(company.id, fiscal_year=fy, metric="eps", on_or_before=before, sources=("nasdaq",))
    if rows and us is not None:
        r = rows[0]
        flags = ["Nasdaq 一致预期多为经调整 EPS，口径可能与报表 EPS 不同"]
        value = r["value"]
        if r["per_ads"]:
            if not us.ads_ratio:
                return None  # 不知道每 ADS 对应几股，无法换成每股
            value /= us.ads_ratio
            flags.append(f"已按每 ADS {us.ads_ratio:g} 股换成每股")
        return _converted(app, r, value, report_ccy, as_of, f"Nasdaq 快照（{r['taken_on']}）", flags)
    return None


def _converted(app: App, r: dict, value: float, report_ccy: str, as_of: date, source: str, flags: list[str]) -> dict | None:
    ccy = r["currency"] or report_ccy
    scale = 1.0
    if ccy != report_ccy:
        rate = valuation.fx_rate(app, ccy, report_ccy, as_of)
        if rate is None:
            log.info("缺少 %s→%s 汇率，无法比较一致预期", ccy, report_ccy)
            return None
        scale = rate[0]
        flags = [*flags, f"按 {rate[1]} 汇率 1 {ccy} = {rate[0]:.4f} {report_ccy} 换算"]
    ratio = value / r["value"] if r["value"] else 1.0  # 每 ADS 换成每股时，区间同比例换算
    low, high = (r["low"] * ratio if r.get("low") is not None else None), (r["high"] * ratio if r.get("high") is not None else None)
    return {"fy": r["fiscal_year"], "metric": r["metric"], "value": value * scale, "mean": None,
            "low": low * scale if low is not None else None, "high": high * scale if high is not None else None,
            "n": r.get("n"), "as_of": r["taken_on"], "currency": report_ccy, "source": source, "flags": flags}


def consensus_for(app: App, company: Company, fy: int, as_of: date, report_ccy: str, cache: ReportCache) -> dict | None:
    """发布前（as_of 之前）的一致预期：A 股按研报还原，否则用快照。"""
    a = company.listing("A")
    if a is not None:
        cfg = app.settings.consensus
        c = a_consensus_at(cache.get(a.code, as_of - timedelta(days=cfg.window_days)), fy, as_of, cfg.window_days)
        snap = _em_snapshot(app, company, fy, as_of)
        # 研报库对部分公司收录很少：样本不足时，改用发布前的机构预测汇总快照（覆盖的机构更多）
        if snap and (c is None or ((c["n"] or 0) < cfg.min_estimates and (snap["n"] or 0) > (c["n"] or 0))):
            c = snap
        if c:
            if company.listing("HK"):
                c["flags"].append("A+H 公司按 A 股口径（人民币）比较")
            return c
    return snapshot_consensus_at(app, company, fy, as_of, report_ccy)


def _em_snapshot(app: App, company: Company, fy: int, as_of: date) -> dict | None:
    """发布前最近一份东方财富机构预测汇总（stock run 每天记录）。"""
    rows = app.db.consensus_rows(company.id, fiscal_year=fy, metric="eps", on_or_before=(as_of - timedelta(days=1)).isoformat(),
                                 sources=("em_a",))
    if not rows:
        return None
    r = rows[0]
    return {"fy": fy, "metric": "eps", "value": r["value"], "mean": None, "low": r.get("low"), "high": r.get("high"),
            "n": r.get("n"), "as_of": r["taken_on"], "currency": "CNY",
            "source": f"东方财富机构预测汇总快照（{r['taken_on']}，{r.get('n') or '—'} 家机构评级）", "flags": []}


# ---------------------------------------------------------------- 对比

def compare(actual: dict, cons: dict | None, history: list[dict], stage: str, min_estimates: int = 3,
            stale_days: int = 90) -> dict:
    """实际值与一致预期对比，返回结构化结果（含给分级与简报用的标题和正文）。"""
    fy, month = period_info(actual["period_end"])
    metric = cons["metric"] if cons else "eps"
    ccy = actual.get("currency") or "CNY"
    res = {"period": actual["period_end"], "label": period_label(actual["period_end"]), "stage": stage,
           "stage_label": STAGE_LABEL[stage], "notice": actual.get("notice"), "fy": fy,
           "kind": "annual" if month == 12 else "interim", "metric": metric, "currency": ccy,
           "actual": actual.get(metric), "actual_low": actual.get(f"{metric}_low"), "actual_high": actual.get(f"{metric}_high"),
           "consensus": cons, "flags": list(actual.get("flags") or [])}
    v = res["actual"]
    if cons is None:
        res["flags"].append("没有发布前的一致预期")
    elif v is None:
        res["flags"].append(f"缺少本期{METRIC_LABEL[metric]}实际值")
    else:
        c = cons["value"]
        loss = c <= 0 or abs(c) < 1e-9
        if res["kind"] == "annual":
            res["diff"] = v - c
            if loss:
                res["flags"].append("一致预期为亏损或接近零：只给差额，不算百分比")
            else:
                res["surprise"] = v / c - 1
        elif loss:
            res["rest"] = c - v
            res["flags"].append("一致预期为亏损或接近零：不算完成度，给出全年一致预期隐含的余下期间数值")
        else:
            res["completion"] = v / c
            norm = seasonal_norm(history, fy, month, metric)
            res["norm"] = norm
            if norm and norm["mean"] > 0:
                if len(norm["years"]) < 2:
                    res["flags"].append("历史同期只有 1 年可比，不按季节性外推全年")
                elif norm["max"] - norm["min"] > NORM_SPREAD:
                    res["flags"].append("历史同期占比波动大，季节性不稳定，不外推全年")
                else:
                    res["implied_fy"] = v / norm["mean"]
                    res["implied_gap"] = res["implied_fy"] / c - 1
            else:
                res["flags"].append("缺少历史同期数据，无法按季节性外推全年")
            if v < 0:
                res["flags"].append("本期为亏损")
        if cons.get("n") is not None and cons["n"] < min_estimates:
            res["flags"].append(f"样本少（{cons['n']} 家）")
        if res["notice"] and cons.get("as_of") and \
                (date.fromisoformat(res["notice"][:10]) - date.fromisoformat(cons["as_of"][:10])).days > stale_days:
            res["flags"].append(f"一致预期可能过时（最新数据 {cons['as_of']}）")
        res["flags"] += cons.get("flags") or []
    res["headline"], res["lines"] = describe(res)
    return res


def describe(res: dict) -> tuple[str, list[str]]:
    """一句话标题与逐条说明（数字全部来自 res）。"""
    m, ccy, cons = res["metric"], res["currency"], res["consensus"]
    name = METRIC_LABEL[m]
    head = f"{res['label']}{'业绩预告' if res['stage'] == 'forecast' else '业绩快报' if res['stage'] == 'express' else ''}"
    actual = show(m, res["actual"], ccy)
    if res["stage"] == "forecast" and res.get("actual_low") is not None:
        actual = f"中点 {actual}（区间 {show(m, res['actual_low'], ccy)}–{show(m, res['actual_high'], ccy)}）"
    if cons is None or res["actual"] is None:
        headline = f"{head} {name} {actual}；{res['flags'][0] if res['flags'] else '无法对比'}"
    elif res["kind"] == "annual":
        cmp_ = f"{pct(res['surprise'])}" if "surprise" in res else f"差额 {show(m, res['diff'], ccy)}"
        headline = f"{head} {name} {actual}，较发布前一致预期（{show(m, cons['value'], ccy)}）{cmp_}"
    elif "rest" in res:
        headline = (f"{head} {name} {actual}；全年一致预期 {show(m, cons['value'], ccy)}，"
                    f"意味着余下期间 {show(m, res['rest'], ccy)}")
    else:
        headline = f"{head} {name} {actual}，完成全年一致预期（{show(m, cons['value'], ccy)}）的 {res['completion']:.0%}"
        if (nm := res.get("norm")) and "implied_fy" in res:
            headline += f"（近 {len(nm['years'])} 年同期平均 {nm['mean']:.0%}）"
        elif nm:
            headline += f"（近 {len(nm['years'])} 年同期占比 " + (f"{nm['min']:.0%}–{nm['max']:.0%}）" if len(nm["years"]) > 1 else f"{nm['mean']:.0%}）")
        if "implied_fy" in res:
            headline += f"，按季节性外推全年 {show(m, res['implied_fy'], ccy)}，较一致预期 {pct(res['implied_gap'])}"
    lines = [f"实际：{res['label']}{res['stage_label']}（{res['notice'] or '发布日未知'} 发布），{name} {actual}（年初至今累计）"
             if res["kind"] == "interim" else
             f"实际：{res['label']}{res['stage_label']}（{res['notice'] or '发布日未知'} 发布），{name} {actual}"]
    if cons is not None:
        spread = (f"，区间 {show(m, cons['low'], ccy)}–{show(m, cons['high'], ccy)}"
                  if cons.get("low") is not None and cons.get("high") is not None else "")
        n = f"{cons['n']} 家" if cons.get("n") else "家数不详"
        lines.append(f"发布前一致预期：{cons['fy']} 年{name} {show(m, cons['value'], ccy)}（{n}{spread}；来源：{cons['source']}）")
    if res.get("norm"):
        nm = res["norm"]
        lines.append(f"历史同期占比（{'、'.join(str(y) for y in nm['years'])} 年）：平均 {nm['mean']:.0%}，区间 {nm['min']:.0%}–{nm['max']:.0%}")
    if res["flags"]:
        lines.append("标注：" + "；".join(res["flags"]))
    return headline, lines


def item_text(res: dict) -> tuple[str, str]:
    """进入分级的数据条目：标题与摘要（摘要以 TAG 开头，表示数字由程序计算）。"""
    return f"业绩对比一致预期｜{res['headline']}"[:200], f"{TAG} " + "；".join(res["lines"])


# ---------------------------------------------------------------- 检查新业绩

def _announcement(app: App, company: Company, listing: Listing, stage: str, day: date | None = None,
                  since: date | None = None) -> dict | None:
    """与业绩对应的公告条目：day 给定时找发布日前后 1 天内的，否则找 since 之后的；取最早的一条。"""
    start = (day - timedelta(days=1)) if day else since
    if start is None:
        return None
    titles = NOTICE_TITLES[stage]
    pattern = re.compile(titles if isinstance(titles, str) else titles.get(listing.market, titles["A"]))
    rows = app.db.items_since(company.id, iso(datetime.combine(start, time(), CST)))
    hits = []
    for r in rows:
        if r["kind"] != "announcement" or r.get("listing") != listing.key:
            continue
        local = parse_iso(r["published_at"]).astimezone(CST).date()
        if day and local > day + timedelta(days=1):
            continue
        text = to_simplified(f"{r['title']} {r.get('source_name') or ''}")
        if pattern.search(text) and not NOT_RESULTS.search(text):
            hits.append(r)
    return min(hits, key=lambda r: r["published_at"]) if hits else None


def _a_candidates(app: App, a: Listing, history: list[dict]) -> list[tuple[str, dict]]:
    """A 股的业绩事件：定期报告（有公告日的报告期）、业绩预告（归母净利区间按股本换成 EPS）、业绩快报。"""
    out = [("report", r) for r in history if r["notice"]]
    shares = shares_estimate(history)
    for f in _try(app, "eastmoney_dc", emdata.a_forecasts, app.session, a.code) or []:
        act = {"period_end": f["period_end"], "notice": f["notice"], "currency": "CNY",
               "profit": (f["low"] + f["high"]) / 2, "profit_low": f["low"], "profit_high": f["high"]}
        if shares:
            act.update({"eps": act["profit"] / shares, "eps_low": f["low"] / shares, "eps_high": f["high"] / shares,
                        "flags": [f"预告的归母净利按股本约 {shares / 1e8:.2f} 亿股换算成 EPS"]})
        out.append(("forecast", act))
    for e in _try(app, "eastmoney_dc", emdata.a_express, app.session, a.code) or []:
        out.append(("express", {"period_end": e["period_end"], "notice": e["notice"], "currency": "CNY",
                                "eps": e["eps"], "profit": e["profit"], "revenue": e["revenue"]}))
    return out


def _record(app: App, company: Company, listing: Listing, stage: str, act: dict, history: list[dict],
            cons: dict | None, notice_dt: datetime, url: str | None) -> dict:
    """保存对比结果；有一致预期时生成一条进入分级的数据条目。"""
    cfg = app.settings.consensus
    res = compare(act, cons, history, stage, cfg.min_estimates, cfg.stale_days)
    uid = None
    if cons is not None and res["actual"] is not None:
        title, snippet = item_text(res)
        x = {"source_id": f"{act['period_end']}:{stage}", "title": title, "snippet": snippet, "url": url,
             "published_at": notice_dt, "earnings": res}
        item = _item(company, company.pricing_listing, "data", SOURCE, x, "一致预期对比")
        app.db.add_item(item)
        uid = item["uid"]
    app.db.save_earnings(company.id, act["period_end"], stage, act.get("notice"), res, uid)
    return res


def _check_a(app: App, company: Company, a: Listing, since: date, cache: ReportCache) -> list[str]:
    fin = _try(app, "eastmoney_dc", emdata.financials, app.session, a, 16)
    if fin is None:
        return [f"✗ {company.name}：没有取到 A 股财务指标，业绩对比稍后重试"]
    history = actual_rows(fin, "A")
    msgs = []
    for stage, act in sorted(_a_candidates(app, a, history), key=lambda x: x[1]["notice"] or ""):
        notice = act.get("notice")
        if not notice or date.fromisoformat(notice) < since or app.db.has_earnings(company.id, act["period_end"], stage):
            continue
        day = date.fromisoformat(notice)
        fy, _ = period_info(act["period_end"])
        cons = consensus_for(app, company, fy, day, "CNY", cache)
        ann = _announcement(app, company, a, stage, day=day)
        notice_dt = parse_iso(ann["published_at"]) if ann else datetime.combine(day, time(18, 0), CST)
        res = _record(app, company, a, stage, act, history, cons, notice_dt, ann.get("url") if ann else None)
        msgs.append(f"{company.name}：{res['headline']}")
    return msgs


def _check_snapshot_market(app: App, company: Company, listing: Listing, state: dict, since: date, first: bool,
                           cache: ReportCache) -> list[str]:
    """港股（及港美两地）：报表没有公告日，以发现新报告期的时间或匹配到的业绩公告为发布日；一致预期来自快照。"""
    fin = _try(app, "eastmoney_dc", emdata.financials, app.session, listing, 12)
    if fin is None:
        return [f"✗ {company.name}：没有取到财务指标，业绩对比稍后重试"]
    history = actual_rows(fin, listing.market)
    if not history:
        return []
    msgs = []
    latest = history[0]
    period = latest["period_end"]
    if period > (state.get("period") or "") and not app.db.has_earnings(company.id, period, "report"):
        ann = _announcement(app, company, listing, "report", since=since)
        if not (first and ann is None):  # 首次检查且最新一期不是近期发布的：只记下水位，不补算旧业绩
            notice_dt = parse_iso(ann["published_at"]) if ann else utcnow()
            state["pending"] = {"period": period, "notice": iso(notice_dt), "url": ann.get("url") if ann else None,
                                "found": today_local().isoformat()}
            state["notified"] = False
        state["period"] = period
    pending = state.get("pending")
    if not pending:
        return msgs
    act = next((r for r in history if r["period_end"] == pending["period"]), None)
    notice_dt = parse_iso(pending["notice"])
    day = notice_dt.astimezone(CST).date()
    if act is None:
        state["pending"] = None
        return msgs
    act = {**act, "notice": day.isoformat()}
    fy, _ = period_info(act["period_end"])
    cons = consensus_for(app, company, fy, day, act["currency"], cache)
    if cons is not None:
        res = _record(app, company, listing, "report", act, history, cons, notice_dt, pending.get("url"))
        state["pending"] = None
        msgs.append(f"{company.name}：{res['headline']}")
    elif (today_local() - date.fromisoformat(pending["found"])).days > PENDING_DAYS:
        _record(app, company, listing, "report", act, history, None, notice_dt, pending.get("url"))
        state["pending"] = None
        msgs.append(f"{company.name}：{period_label(act['period_end'])} 已发布，{PENDING_DAYS} 天内没有补录发布前的一致预期，不再等待")
    elif not state.get("notified"):
        state["notified"] = True
        msgs.append(f"{company.name}：{period_label(act['period_end'])} 已发布（{day}），但没有发布前的一致预期快照——"
                    f"可在公司页或用 stock consensus set 补录发布日之前的一致预期，下次运行自动对比")
    return msgs


def check_company(app: App, company: Company, cache: ReportCache) -> list[str]:
    key = f"earnings:{company.id}"
    state = json.loads(app.db.kv_get(key) or "{}")
    first = not state
    since = date.fromisoformat(state["since"]) if state.get("since") else \
        today_local() - timedelta(days=app.settings.consensus.lookback_days)
    state["since"] = since.isoformat()
    if a := company.listing("A"):
        msgs = _check_a(app, company, a, since, cache)
    elif listing := company.listing("HK"):
        msgs = _check_snapshot_market(app, company, listing, state, since, first, cache)
    else:
        msgs = []  # 只在美股上市的公司：没有实际值来源（SEC 季度数据另行处理），暂不对比
    app.db.kv_set(key, json.dumps(state, ensure_ascii=False))
    return msgs


def check(app: App, companies: list[Company]) -> list[str]:
    """检查新发布的业绩并与发布前的一致预期对比（stock run 每次运行；每家公司 1–3 个请求）。"""
    if not app.settings.consensus.enabled:
        return []
    cache = ReportCache(app)
    msgs = []
    for c in companies:
        try:
            msgs += check_company(app, c, cache)
        except Exception as e:  # noqa: BLE001 - 单家公司失败不影响其他
            log.warning("%s 业绩对比失败：%s", c.name, e)
            msgs.append(f"✗ {c.name} 业绩对比失败：{type(e).__name__}: {str(e)[:120]}")
    return msgs


def snapshot(app: App, companies: list[Company]) -> list[str]:
    """每天记录一次当前一致预期（A 股：东方财富机构预测；美股：Nasdaq）。港股只有使用者录入的快照。"""
    if not app.settings.consensus.enabled:
        return []
    today = today_local().isoformat()
    saved = 0
    for c in companies:
        key = f"consensus_checked:{c.id}"
        if app.db.kv_get(key) == today:
            continue
        n = 0
        if a := c.listing("A"):
            cur = _try(app, "eastmoney_dc", emdata.a_consensus, app.session, a.code)
            if cur:
                n += app.db.save_consensus(c.id, "em_a", today, [
                    {"fiscal_year": y["fy"], "metric": "eps", "value": y["eps"], "currency": "CNY", "n": cur["orgs"]}
                    for y in cur["years"] if not y["actual"]], listing=a.key)
        if us := c.listing("US"):
            eps = _try(app, "nasdaq", nasdaq.yearly_eps, app.session, us.code)
            if eps:
                n += app.db.save_consensus(c.id, "nasdaq", today, [
                    {"fiscal_year": e["fy"], "metric": "eps", "value": e["avg"], "currency": "USD", "per_ads": bool(us.ads_ratio),
                     "n": e["n"], "high": e["high"], "low": e["low"]}
                    for e in eps if e["fy"] and e["avg"] is not None], listing=us.key)
        app.db.kv_set(key, today)
        saved += bool(n)
    return [f"一致预期快照：{saved} 家"] if saved else []


def save_manual(app: App, company: Company, fiscal_year: int, metric: str, value: float, currency: str,
                taken_on: str | None = None, n: int | None = None, source: str | None = None,
                high: float | None = None, low: float | None = None) -> str:
    """使用者录入一致预期（港股没有免费数据源）。归母净利与营收按「元」保存（录入时单位为亿）。"""
    if metric not in METRIC_LABEL:
        raise ValueError(f"指标只能是 {' / '.join(METRIC_LABEL)}")
    if not 2000 <= int(fiscal_year) <= today_local().year + 5:
        raise ValueError(f"财年不合法：{fiscal_year}")
    if currency not in CCY_UNIT:
        raise ValueError("币种只能是 CNY / HKD / USD")
    day = date.fromisoformat(taken_on) if taken_on else today_local()
    if day > today_local():
        raise ValueError("数据日期不能晚于今天")
    scale = 1.0 if metric == "eps" else 1e8
    app.db.save_consensus(company.id, "manual", day.isoformat(), [{
        "fiscal_year": int(fiscal_year), "metric": metric, "value": float(value) * scale, "currency": currency, "n": n,
        "high": high * scale if high is not None else None, "low": low * scale if low is not None else None,
        "note": (source or "").strip()[:60] or None}], listing=company.pricing_listing.key)
    return (f"已保存：{company.name} {fiscal_year} 年{METRIC_LABEL[metric]}一致预期 {show(metric, float(value) * scale, currency)}"
            f"（数据日期 {day}{f'，{n} 家' if n else ''}{f'，来源 {source}' if source else ''}）")


# ---------------------------------------------------------------- 工具与 CLI 的展示

def a_history(app: App, a: Listing, n: int, cache: ReportCache) -> list[dict]:
    """A 股近 n 个报告期的「实际 vs 发布前一致预期」（按研报库回溯，不写库）。"""
    fin = _try(app, "eastmoney_dc", emdata.financials, app.session, a, max(16, n + 12))
    if not fin:
        return []
    history = actual_rows(fin, "A")
    cfg = app.settings.consensus
    periods = [r for r in history if r["notice"]][:n]
    if not periods:
        return []
    reports = cache.get(a.code, min(date.fromisoformat(r["notice"]) for r in periods) - timedelta(days=cfg.window_days))
    out = []
    for act in periods:
        day = date.fromisoformat(act["notice"])
        fy, _ = period_info(act["period_end"])
        cons = a_consensus_at(reports, fy, day, cfg.window_days)
        out.append(compare(act, cons, history, "report", cfg.min_estimates, cfg.stale_days))
    return out


def trend_lines(app: App, company: Company | None, a: Listing | None, cache: ReportCache) -> list[str]:
    """当前一致预期与修正趋势：A 股按研报还原今天、30 天前、90 天前的中位数；其他市场列出最新快照。"""
    lines = []
    today = today_local()
    if a is not None:
        cur = _try(app, "eastmoney_dc", emdata.a_consensus, app.session, a.code)
        if cur:
            est = [y for y in cur["years"] if not y["actual"]]
            lines.append(f"{a.display} 当前一致预期（东方财富机构预测，{cur['orgs']} 家机构评级）：" +
                         "；".join(f"{y['fy']} 年 EPS {y['eps']:.2f} 元" for y in est))
        window = app.settings.consensus.window_days
        reports = cache.get(a.code, today - timedelta(days=90 + window))
        for fy in (today.year, today.year + 1):
            pts = [(k, a_consensus_at(reports, fy, today - timedelta(days=k) + timedelta(days=1), window)) for k in (0, 30, 90)]
            if pts[0][1]:
                now = pts[0][1]["value"]
                chg = "；".join(f"{k} 天前 {c['value']:.2f}（变化 {pct(now / c['value'] - 1)}）" for k, c in pts[1:]
                               if c and c["value"] > 0 and now > 0)
                lines.append(f"{fy} 年 EPS 研报中位数（近 {window} 天每家机构最新一篇）：现在 {now:.2f}（{pts[0][1]['n']} 家）"
                             + (f"；{chg}" if chg else ""))
    if company is not None:
        snaps = [r for r in app.db.consensus_latest(company.id) if r["source"] != "em_a" or a is None]
        for r in snaps:
            unit = "（每 ADS）" if r["per_ads"] else ""
            lines.append(f"{SNAPSHOT_LABEL.get(r['source'], r['source'])}（{r['taken_on']}）：{r['fiscal_year']} 年 {METRIC_LABEL[r['metric']]}"
                         f"{unit} {show(r['metric'], r['value'], r['currency'] or 'CNY')}"
                         + (f"（{r['n']} 家）" if r["n"] else "") + (f"，来源 {r['note']}" if r["note"] else ""))
    return lines


def report_text(app: App, company: Company | None, listing: Listing | None = None, n: int = 6) -> str:
    """earnings_vs_consensus 工具与 stock consensus show 的输出。"""
    cache = ReportCache(app)
    a = company.listing("A") if company is not None else (listing if listing and listing.market == "A" else None)
    name = company.name if company is not None else (listing.display if listing else "")
    out = [f"{name} 业绩 vs 一致预期（程序计算）"]
    rows = a_history(app, a, n, cache) if a is not None else \
        ([r["data"] for r in app.db.earnings(company.id, n)] if company is not None else [])
    trend = trend_lines(app, company, a, cache)  # 研报在上一步按更宽的时间窗抓过，这里复用
    out += ["", "当前一致预期：", *[f"- {x}" for x in trend]] if trend else ["", "当前一致预期：没有数据" +
                                                                         ("（港股请在公司页录入）" if a is None else "")]
    if rows:
        out += ["", "历史各期：实际 vs 发布前一致预期"]
        for r in rows:
            extra = [f for f in r["flags"] if f not in r["headline"]]
            out.append(f"- {r['headline']}" + (f"（{'；'.join(extra)}）" if extra else ""))
    else:
        out += ["", "历史各期：暂无记录" + ("（港股需要在业绩发布前录入一致预期，发布后由 stock run 自动对比）" if a is None else "")]
    us = company.listing("US") if company is not None else (listing if listing and listing.market == "US" else None)
    if us is not None:
        surprises = _try(app, "nasdaq", nasdaq.earnings_surprise, app.session, us.code) or []
        known = [s for s in surprises if s["consensus"] is not None]
        if known:
            out += ["", f"{us.display} 季度业绩惊喜（Nasdaq，多为经调整 EPS）："]
            out += [f"- {s['quarter']}（{s['reported']} 发布）：实际 {s['eps']}，当时一致预期 {s['consensus']}"
                    + (f"，惊喜 {s['surprise_pct']:+.1f}%" if s["surprise_pct"] is not None else "") for s in known]
    out.append("")
    out.append("说明：A 股按研报库还原发布前 120 天内每家机构最新一篇的 EPS 中位数；中报、季报为年初至今累计，"
               "看完成全年一致预期的比例；近 3 年同期占比稳定时才按它外推全年，占比忽高忽低（如周期股）时只列历史区间。")
    return "\n".join(out)
