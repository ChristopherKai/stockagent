"""前瞻催化剂日历：用结构化数据推算未来一段时间可能发生的事件（代码计算，不靠模型猜测）。

- 分红：已公告、尚未除息的派息（确定）；按往年同期除息日推算的下一次派息（预计，要求连续派息）；
  连续派息年数与近 12 个月股息率（A 股由每 10 股派息推算；港股用主要指标里的股息率）。
- 业绩窗口：A 股定期报告预约披露日；港股按财年推算的法定披露期限（中期报告期末后 2 个月、年度 3 个月）。
  只有业绩趋势向上（预期修正桶入选）时才算正面催化剂，否则只是提示。
- A 股限售解禁：未来 120 天解禁占总股本 ≥ 3% 或占流通股 ≥ 5% 的，作为负面标记。
- 深度报告里核实过的催化剂（观察清单里的公司）：直接纳入。
模型在快筛里推测的催化剂只展示，不参与分层。
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Callable

from .. import monitor
from ..app import App, _try
from ..sources import emscreen
from ..utils import to_float, today_local
from . import store
from .universe import SOURCE, _bulk_cached, session

LIFT_DAYS = 120


def _d(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def refresh(app: App, markets: list[str] | None = None, force: bool = False,
            progress: Callable[[str], None] = lambda m: None) -> list[str]:
    markets = markets or app.settings.screen.markets
    s = session(app)
    msgs = []
    today = today_local()
    if "HK" in markets:
        codes = [u["code"] for u in store.universe(app.db, "HK")]
        if codes:
            progress("港股分红记录…")
            since = (today - timedelta(days=5 * 366)).isoformat()
            rows = _bulk_cached(app, "HK:div", force, 1,
                                lambda: _flat(_try(app, SOURCE, emscreen.hk_dividends, s, codes, since)))
            msgs.append(f"港股分红记录 {len(rows)} 条")
    if "A" in markets:
        progress("A 股分红、解禁、预约披露…")
        n = 0
        for p in dividend_periods(today):
            fresh = app.settings.screen.refresh_days if _d(p) > today - timedelta(days=500) else 10_000  # 早年的分红不会再变
            n += len(_bulk_cached(app, f"A:div:{p}", force, fresh, lambda p=p: _try(app, SOURCE, emscreen.a_dividends, s, p)))
        lifts = _bulk_cached(app, "A:lift", force, 1,
                             lambda: _try(app, SOURCE, emscreen.a_lifts, s, today, today + timedelta(days=LIFT_DAYS)))
        appt = _bulk_cached(app, f"A:appt:{next_period(today)}", force, 1,
                            lambda: _try(app, SOURCE, emscreen.a_appointments, s, next_period(today)))
        msgs.append(f"A 股分红方案 {n} 条、{LIFT_DAYS} 天内解禁 {len(lifts)} 条、预约披露 {len(appt)} 条")
    return msgs


def _flat(d: dict | None) -> list[dict] | None:
    return None if d is None else [r for rows in d.values() for r in rows]


def dividend_periods(today: date) -> list[str]:
    """近 5 个财年的年报与中报（A 股分红按报告期登记）。"""
    return [f"{y}-{md}" for y in range(today.year - 5, today.year + 1) for md in ("06-30", "12-31") if f"{y}-{md}" < today.isoformat()]


def next_period(today: date) -> str:
    """正在披露或即将披露的定期报告期：今天往后 31 天以内的最近一个季末（10 月看三季报，4 月看一季报与年报）。"""
    horizon = (today + timedelta(days=31)).isoformat()
    return max(f"{y}-{md}" for y in (today.year - 1, today.year) for md in ("03-31", "06-30", "09-30", "12-31")
               if f"{y}-{md}" <= horizon)


def _half_year_later(d: date) -> date:
    y, m = (d.year + 1, d.month - 6) if d.month > 6 else (d.year, d.month + 6)
    nxt = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)  # 该月最后一天
    return nxt


# ---------------------------------------------------------------- 按标的汇总

def _hk_year(period: str) -> int | None:
    m = re.match(r"(\d{4})", period or "")
    return int(m.group(1)) if m else None


def _consecutive(years: set[int], latest_fy: int) -> int:
    n, y = 0, latest_fy
    while y in years:
        n += 1
        y -= 1
    return n


def dividends_hk(rows: list[dict], today: date) -> dict:
    paid = [r for r in rows if r.get("IS_DIVIDEND") == "1" and "不派" not in (r.get("PLAN_EXPLAIN") or "")]
    years = {y for r in paid if (y := _hk_year(r.get("ASSIGN_PERIOD")))}
    exd = sorted(d for r in paid if (d := _d(r.get("EX_DIVIDEND_DATE"))))
    upcoming = [(d, r) for r in paid if (d := _d(r.get("EX_DIVIDEND_DATE"))) and d > today]
    return {"years": _consecutive(years, today.year - 1) or _consecutive(years, today.year), "past_ex": [d for d in exd if d <= today],
            "upcoming": [{"date": d.isoformat(), "text": f"{r.get('ASSIGN_PERIOD')} {r.get('PLAN_EXPLAIN') or ''}".strip()} for d, r in upcoming],
            "plans": []}


def dividends_a(rows: list[dict], price: float | None, today: date) -> dict:
    cash = [r for r in rows if (to_float(r.get("PRETAX_BONUS_RMB")) or 0) > 0]
    years = {int(r["REPORT_DATE"][:4]) for r in cash}
    past = [(d, r) for r in cash if (d := _d(r.get("EX_DIVIDEND_DATE"))) and d <= today]
    ttm = sum(to_float(r["PRETAX_BONUS_RMB"]) / 10 for d, r in past if d > today - timedelta(days=365))
    upcoming = [{"date": d.isoformat(), "text": r.get("IMPL_PLAN_PROFILE") or ""} for r in cash
                if (d := _d(r.get("EX_DIVIDEND_DATE"))) and d > today]
    plans = [{"date": (r.get("PLAN_NOTICE_DATE") or r.get("NOTICE_DATE") or "")[:10], "text": f"{r['REPORT_DATE'][:10]} 分配预案 {r.get('IMPL_PLAN_PROFILE') or ''}"}
             for r in cash if not r.get("EX_DIVIDEND_DATE") and "预案" in (r.get("ASSIGN_PROGRESS") or "")]
    return {"years": _consecutive(years, today.year - 1), "past_ex": sorted(d for d, _ in past), "upcoming": upcoming, "plans": plans,
            "dividend_yield": ttm / price if price and ttm else None}


def build(app: App, rows: list[dict]) -> dict[str, dict]:
    """secid → {"dividend": {...}, "items": [{date, kind, direction, confirmed, text}], "flags": [...]}（只用库里的缓存）。"""
    today = today_local()
    out: dict[str, dict] = {r["secid"]: {"dividend": None, "items": [], "flags": []} for r in rows}
    by_code = {(r["market"], r["code"]): r for r in rows}
    hk = defaultdict(list)
    for r in (store.bulk_get(app.db, "HK:div") or (None, []))[1]:
        hk[emscreen.hk_code(r["SECUCODE"])].append(r)
    a = defaultdict(list)
    for p in dividend_periods(today):
        for r in (store.bulk_get(app.db, f"A:div:{p}") or (None, []))[1]:
            a[r["SECURITY_CODE"]].append(r)
    for (market, code), u in by_code.items():
        div = dividends_hk(hk.get(code, []), today) if market == "HK" else dividends_a(a.get(code, []), u.get("price"), today)
        c = out[u["secid"]]
        c["dividend"] = div
        for x in div["upcoming"]:
            c["items"].append({"date": x["date"], "kind": "dividend", "direction": "positive", "confirmed": True,
                               "text": f"已公告派息，{x['date']} 除息：{x['text'][:40]}"})
        for x in div["plans"]:
            c["items"].append({"date": today.isoformat(), "kind": "dividend", "direction": "positive", "confirmed": True,
                               "text": x["text"][:60]})
        if div["years"] >= 3 and not div["upcoming"]:  # 按往年同期推算下一次除息
            nxt = sorted(d.replace(year=d.year + 1) for d in div["past_ex"] if today - timedelta(days=365) < d <= today
                         and not (d.month == 2 and d.day == 29))
            nxt = [d for d in nxt if d > today]
            if nxt:
                c["items"].append({"date": nxt[0].isoformat(), "kind": "dividend", "direction": "positive", "confirmed": False,
                                   "text": f"预计 {nxt[0]:%Y-%m} 前后除息（连续 {div['years']} 年派息，按去年同期推算）"})
    _results_windows(app, out, by_code, today)
    _lifts(app, out, by_code, today)
    _dossier_catalysts(app, out, by_code, today)
    for c in out.values():
        c["items"].sort(key=lambda x: x["date"])
    return out


def _results_windows(app: App, out: dict, by_code: dict, today: date) -> None:
    for r in (store.bulk_get(app.db, f"A:appt:{next_period(today)}") or (None, []))[1]:
        u = by_code.get(("A", r.get("SECURITY_CODE")))
        d = _d(r.get("APPOINT_PUBLISH_DATE"))
        if u and d and d >= today and r.get("IS_PUBLISH") != "1":
            out[u["secid"]]["items"].append({"date": d.isoformat(), "kind": "results", "direction": "neutral", "confirmed": True,
                                             "text": f"预约 {d:%m-%d} 披露{(r.get('REPORT_TYPE_NAME') or '').replace(' ', '')}"})
    for (market, _), u in by_code.items():
        if market != "HK":
            continue
        rd = _d((u.get("info") or {}).get("report_date"))
        if not rd:
            continue
        end = _half_year_later(rd)  # 港股按半年报告：下一个报告期末
        fy = ((u.get("info") or {}).get("fiscal_year") or "12-31")[-5:]
        annual = end.strftime("%m-%d") == fy
        due = end + timedelta(days=92 if annual else 61)
        if today < due <= today + timedelta(days=200):
            out[u["secid"]]["items"].append({"date": due.isoformat(), "kind": "results", "direction": "neutral", "confirmed": False,
                                             "text": f"{end:%Y-%m} {'年度' if annual else '中期'}业绩最迟 {due:%Y-%m-%d} 前披露（法定期限）"})


def _lifts(app: App, out: dict, by_code: dict, today: date) -> None:
    for r in (store.bulk_get(app.db, "A:lift") or (None, []))[1]:
        u = by_code.get(("A", r.get("SECURITY_CODE")))
        tot, flt = to_float(r.get("TOTAL_RATIO")) or 0, to_float(r.get("FREE_RATIO")) or 0
        if not u or (tot < 0.03 and flt < 0.05):
            continue
        d = r["FREE_DATE"][:10]
        sev = "high" if (tot >= 0.1 or flt >= 0.2) and _d(d) <= today + timedelta(days=60) else "mid"
        out[u["secid"]]["flags"].append({"code": "unlock", "severity": sev,
                                         "text": f"{d} 解禁 {r.get('FREE_SHARES_TYPE') or ''}：占总股本 {tot:.1%}、占流通股 {flt:.1%}"})
        out[u["secid"]]["items"].append({"date": d, "kind": "unlock", "direction": "negative", "confirmed": True,
                                         "text": f"限售解禁占总股本 {tot:.1%}"})


def _dossier_catalysts(app: App, out: dict, by_code: dict, today: date) -> None:
    """观察清单里有深度报告的公司：报告里核实过、有具体日期的催化剂。"""
    for c in app.watchlist.companies:
        items = monitor.upcoming(app, c, days=365)
        if not items:
            continue
        for l in c.listings:
            u = by_code.get((l.market, l.code))
            if u is None:
                continue
            for x in items:
                if _d(x["due_date"]) and _d(x["due_date"]) >= today:
                    out[u["secid"]]["items"].append({"date": x["due_date"], "kind": "dossier", "direction": "positive", "confirmed": True,
                                                     "text": f"深度报告催化剂：{x['event'][:50]}"})


def window(cal: dict | None, days: int) -> list[dict]:
    """某个时间窗内的日历事件（今天起）。"""
    if not cal:
        return []
    end = (today_local() + timedelta(days=days)).isoformat()
    return [x for x in cal["items"] if x["date"] <= end]
