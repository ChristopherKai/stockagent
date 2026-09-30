"""L0 股票池与报表数据：港股通 + A 股，全部来自东方财富数据中心的批量接口（不逐只请求）。

- 港股：港股通名单 → 公司资料（行业、机构类型、核数师）→ 主要指标（当前总市值、股息率、利润增速、毛利率）
  → 现金流量表 / 资产负债表所需科目（按代码分批）。
- A 股：最新交易日估值快照（市值、价格、行业）→ 按报告期取全市场现金流量表、资产负债表、主要指标（缓存；
  已披露一年以上的报告期只抓一次）。
报表数据与 fcf_yield 工具同一口径（emdata.hk_fcf_from_rows / a_fcf_from_rows）。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Callable

from ..app import App, _try
from ..sources import emscreen
from ..sources.emdata import a_fcf_from_rows, infer_hk_currency
from ..utils import log, make_session, parse_iso, to_float, today_local, utcnow
from . import store

SOURCE = "eastmoney_dc"  # 与单公司报表查询共用熔断


def session(app: App):
    return make_session(app.settings.screen.request_delay)


def refresh(app: App, markets: list[str] | None = None, force: bool = False,
            progress: Callable[[str], None] = lambda m: None) -> list[str]:
    msgs: list[str] = []
    for m in markets or app.settings.screen.markets:
        fn = refresh_hk if m == "HK" else refresh_a
        try:
            msgs += fn(app, force=force, progress=progress)
        except Exception as e:  # noqa: BLE001 - 一个市场失败不影响另一个
            log.warning("股票池 %s 刷新失败：%s", m, e)
            msgs.append(f"✗ {m} 股票池刷新失败：{type(e).__name__}: {str(e)[:200]}")
    return msgs


def _stale(ts: str | None, days: int) -> bool:
    return not ts or utcnow() - parse_iso(ts) > timedelta(days=days)


def _same_period(rows: list[dict], latest: dict, field: str) -> dict | None:
    d = latest[field][:10]
    prev = f"{int(d[:4]) - 1}{d[4:]}"
    return next((r for r in rows if (r.get(field) or "")[:10] == prev), None)


# ---------------------------------------------------------------- 港股

def refresh_hk(app: App, force: bool = False, progress: Callable[[str], None] = lambda m: None) -> list[str]:
    s = session(app)
    progress("港股通名单…")
    connect = _try(app, SOURCE, emscreen.hk_connect, s)
    if not connect:
        return ["✗ 没有取到港股通名单（东方财富数据中心不可用？）"]
    codes = sorted(connect)
    progress(f"港股通 {len(codes)} 只：公司资料与主要指标…")
    profiles = _try(app, SOURCE, emscreen.hk_profiles, s, codes) or {}
    since = (today_local() - timedelta(days=800)).isoformat()
    inds = _try(app, SOURCE, emscreen.hk_indicators, s, codes, since) or {}
    rows = []
    for code in codes:
        c, p, ind = connect[code], profiles.get(code, {}), inds.get(code) or []
        latest = ind[0] if ind else {}
        prev = _same_period(ind, latest, "STD_REPORT_DATE") if latest else None
        mcap, shares = to_float(latest.get("TOTAL_MARKET_CAP")), to_float(latest.get("ISSUED_COMMON_SHARES"))
        rows.append({
            "secid": f"116.{code}", "market": "HK", "code": code, "name": c["name"], "org_code": c.get("org_code"),
            "industry": p.get("industry"), "org_type": p.get("org_type") or latest.get("ORG_TYPE"), "st": 0,
            "listing_date": p.get("listing_date") or c.get("listing_date"),
            "price": mcap / shares if mcap and shares else None, "mcap": mcap, "currency": "HKD",
            "report_ccy": infer_hk_currency(ind) if ind else None,
            "pe_ttm": to_float(latest.get("PE_TTM")), "pb": to_float(latest.get("PB_TTM")), "quote_date": today_local().isoformat(),
            "info": {
                "connect": "、".join(k for k, v in (("沪", c["sh"]), ("深", c["sz"])) if v), "security_type": c.get("security_type"),
                "industry_type": p.get("industry_type"), "auditor": p.get("auditor"),
                "report_date": (latest.get("STD_REPORT_DATE") or "")[:10] or None,
                "dividend_yield": _pct(latest.get("DIVIDEND_RATE")), "payout": to_float(latest.get("DIVI_RATIO")),
                "profit_yoy": to_float(latest.get("HOLDER_PROFIT_YOY")), "revenue_yoy": to_float(latest.get("OPERATE_INCOME_YOY")),
                "gross_margin": to_float(latest.get("GROSS_PROFIT_RATIO")),
                "gross_margin_prev": to_float(prev.get("GROSS_PROFIT_RATIO")) if prev else None,
                "roe": to_float(latest.get("ROE_YEARLY")), "debt_ratio": to_float(latest.get("DEBT_ASSET_RATIO")),
                "fiscal_year": latest.get("FISCAL_YEAR"),
            },
        })
    store.upsert_universe(app.db, rows, "HK")
    msgs = [f"港股通股票池 {len(rows)} 只（有主要指标 {sum(1 for r in rows if r['mcap'])} 只）"]
    last = [u["fcf_at"] for u in store.universe(app.db, "HK") if u.get("fcf_at")]
    if force or len(last) < len(rows) * 0.8 or _stale(min(last, default=None), app.settings.screen.refresh_days):
        progress("港股报表（现金流量表、资产负债表）…")
        cf_since = (today_local() - timedelta(days=4 * 365 + 200)).isoformat()
        bs_since = (today_local() - timedelta(days=500)).isoformat()
        data = _try(app, SOURCE, emscreen.hk_statements, s, codes, cf_since, bs_since) or {}
        store.set_fcf(app.db, {f"116.{c}": d for c, d in data.items()})
        msgs.append(f"港股报表 {len(data)} 家")
    return msgs


def _pct(v) -> float | None:
    x = to_float(v)
    return x / 100 if x is not None else None


# ---------------------------------------------------------------- A 股

def _secid_a(secucode: str) -> tuple[str, str]:
    code, ex = secucode.split(".")
    return (f"1.{code}" if ex == "SH" else f"0.{code}"), code


def quarter_ends(before: date, n: int = 4) -> list[str]:
    out, y = [], before.year
    while len(out) < n:
        for md in ("12-31", "09-30", "06-30", "03-31"):
            d = f"{y}-{md}"
            if d < before.isoformat() and len(out) < n:
                out.append(d)
        y -= 1
    return out


def latest_period(app: App, s, universe_size: int) -> str | None:
    """已有七成以上公司披露现金流量表的最新报告期。"""
    for p in quarter_ends(today_local(), 4):
        n = _try(app, SOURCE, emscreen.a_reported, s, p)
        if n is None:
            return None
        if n >= universe_size * 0.7:
            return p
    return None


def a_periods(latest: str) -> dict[str, list[str]]:
    y, md = int(latest[:4]), latest[5:]
    fy = lambda k: f"{y - k}-12-31"
    if md == "12-31":
        cf = [latest, fy(1), fy(2)]
    else:
        cf = [latest, fy(1), f"{y - 1}-{md}", fy(2), fy(3)]
    return {"cf": cf, "bs": [latest, fy(1) if md != "12-31" else f"{y}-09-30"], "ind": [latest, f"{y - 1}-{md}"]}


def _bulk_cached(app: App, key: str, force: bool, max_age_days: float, fetch: Callable[[], list[dict] | None]) -> list[dict]:
    """全市场批量数据的缓存（screen_bulk）：未过期直接用；抓取失败时沿用旧缓存。"""
    cached = store.bulk_get(app.db, key)
    if cached and not force and not _stale(cached[0], max_age_days):
        return cached[1]
    rows = fetch()
    if rows is None:
        return cached[1] if cached else []
    store.bulk_set(app.db, key, rows)
    return rows


def _bulk(app: App, s, kind: str, period: str, force: bool) -> list[dict]:
    old = date.fromisoformat(period) < today_local() - timedelta(days=400)  # 很久以前的报告期不会再变
    return _bulk_cached(app, f"A:{kind}:{period}", force, 10_000 if old else app.settings.screen.refresh_days,
                        lambda: _try(app, SOURCE, emscreen.a_statements, s, kind, period))


def refresh_a(app: App, force: bool = False, progress: Callable[[str], None] = lambda m: None) -> list[str]:
    s = session(app)
    progress("A 股估值快照…")
    got = _try(app, SOURCE, emscreen.a_valuation, s)
    if not got or not got[1]:
        return ["✗ 没有取到 A 股估值快照（东方财富数据中心不可用？）"]
    day, val = got
    val = [r for r in val if (r.get("SECUCODE") or "").endswith((".SH", ".SZ", ".BJ"))]
    latest = latest_period(app, s, len(val))
    if not latest:
        return ["✗ 无法确定 A 股最新报告期"]
    periods = a_periods(latest)
    progress(f"A 股 {len(val)} 只，最新报告期 {latest}：批量报表…")
    by: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for kind, ps in periods.items():
        for p in ps:
            for r in _bulk(app, s, kind, p, force):
                by[r["SECUCODE"]][kind].append(r)
    rows, fcf = [], {}
    for v in val:
        secid, code = _secid_a(v["SECUCODE"])
        d = by.get(v["SECUCODE"], {})
        ind = sorted(d.get("ind", []), key=lambda r: r["REPORT_DATE"], reverse=True)
        last = ind[0] if ind else {}
        prev = _same_period(ind, last, "REPORT_DATE") if last else None
        name = v.get("SECURITY_NAME_ABBR") or ""
        rows.append({
            "secid": secid, "market": "A", "code": code, "name": name, "org_code": v.get("ORG_CODE"),
            "industry": v.get("BOARD_NAME"), "org_type": last.get("ORG_TYPE"), "st": int("ST" in name.upper()),
            "listing_date": None, "price": to_float(v.get("CLOSE_PRICE")), "mcap": to_float(v.get("TOTAL_MARKET_CAP")),
            "currency": "CNY", "report_ccy": "CNY", "pe_ttm": to_float(v.get("PE_TTM")), "pb": to_float(v.get("PB_MRQ")),
            "quote_date": day,
            "info": {
                "report_date": (last.get("REPORT_DATE") or "")[:10] or None, "notice_date": (last.get("NOTICE_DATE") or "")[:10] or None,
                "profit_yoy": to_float(last.get("PARENTNETPROFITTZ")), "revenue_yoy": to_float(last.get("TOTALOPERATEREVETZ")),
                "gross_margin": to_float(last.get("XSMLL")), "gross_margin_prev": to_float(prev.get("XSMLL")) if prev else None,
                "roe": to_float(last.get("ROEJQ")), "debt_ratio": to_float(last.get("ZCFZL")),
                "pcf_ocf_ttm": to_float(v.get("PCF_OCF_TTM")),
            },
        })
        bs = sorted(d.get("bs", []), key=lambda r: r["REPORT_DATE"], reverse=True)
        cf = d.get("cf", [])
        if bs and cf:
            fcf[secid] = a_fcf_from_rows(cf, bs[0])
    store.upsert_universe(app.db, rows, "A")
    store.set_fcf(app.db, fcf)
    return [f"A 股股票池 {len(rows)} 只（{day}），报表 {len(fcf)} 家，最新报告期 {latest}"]


def age_days(u: dict) -> int | None:
    if not u.get("listing_date"):
        return None
    try:
        return (today_local() - date.fromisoformat(u["listing_date"][:10])).days
    except ValueError:
        return None
