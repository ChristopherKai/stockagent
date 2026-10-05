"""美股结构化数据：由 SEC XBRL（companyfacts）派生 EV/FCF 所需报表、估值序列所需的 EPS/BPS、主要财务指标，
以及 Nasdaq / FINRA / SEC 的股东、内部人与做空数据。全部由代码计算；报表金额单位为报告币种的元（与 emdata.fcf_data 同形状）。"""

from __future__ import annotations

import json
from datetime import date, timedelta

from .app import App, _try
from .config import Listing
from .sources import finra, ids, nasdaq, sec
from .utils import num, pct, today_local, utcnow

# 概念候选（靠前优先）；us-gaap 与 ifrs-full 各一套
CONCEPTS = {
    "us-gaap": {
        "ocf": ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
        "inv": ["NetCashProvidedByUsedInInvestingActivities", "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations"],
        "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
                  "PaymentsToAcquireOtherPropertyPlantAndEquipment"],
        "software": ["PaymentsForSoftware", "PaymentsToDevelopSoftware"],
        "cash": ["CashAndCashEquivalentsAtCarryingValue"],
        "invest": ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
        "restricted": ["RestrictedCashCurrent", "RestrictedCashAndCashEquivalentsAtCarryingValue"],
        "debt_short": ["ShortTermBorrowings", "LineOfCredit"],
        "debt_cur": ["DebtCurrent", "LongTermDebtCurrent", "ConvertibleNotesPayableCurrent"],
        "debt_long": ["LongTermDebtNoncurrent", "ConvertibleNotesPayableNoncurrent", "LongTermNotesPayable", "ConvertibleDebtNoncurrent"],
        "debt_total": ["LongTermDebt"],
        "minority": ["MinorityInterest"], "liabilities": ["Liabilities"],
        "eps": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"],
        "equity": ["StockholdersEquity"], "shares_out": ["CommonStockSharesOutstanding"],
        "ni": ["NetIncomeLoss", "ProfitLoss"],
        "wshares": ["WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfSharesOutstandingBasic"],
    },
    "ifrs-full": {
        "ocf": ["CashFlowsFromUsedInOperatingActivities"], "inv": ["CashFlowsFromUsedInInvestingActivities"],
        "capex": ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"],
        "software": ["PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities"],
        "cash": ["CashAndCashEquivalents"], "invest": ["OtherCurrentFinancialAssets"], "restricted": [],
        "debt_short": [], "debt_cur": ["CurrentBorrowings"], "debt_long": ["NoncurrentBorrowings"], "debt_total": ["Borrowings"],
        "minority": ["NoncontrollingInterests"], "liabilities": ["Liabilities"],
        "eps": ["DilutedEarningsLossPerShare", "BasicEarningsLossPerShare"], "equity": ["EquityAttributableToOwnersOfParent"],
        "shares_out": ["NumberOfSharesOutstanding"], "ni": ["ProfitLossAttributableToOwnersOfParent", "ProfitLoss"],
        "wshares": ["WeightedAverageShares"],
    },
}


def load_facts(app: App, listing: Listing) -> tuple[dict | None, str]:
    """(companyfacts, 提示)。facts 为 None 时提示是原因；XBRL 落后于已申报的 10-K / 10-Q 时先绕过缓存重取一次，
    仍落后则提示（最新一期请看申报原文）。"""
    ua = app.settings.sources.sec_user_agent
    if not ua:
        return None, "美股数据来自 SEC XBRL，需要先在 config.yaml 设置 sources.sec_user_agent（「姓名 邮箱」）"
    cik = ids.sec_cik(app.db, app.session, listing, ua)
    if not cik:
        return None, "SEC 找不到这个代码"
    facts = sec.company_facts(app.session, cik, ua)
    note = ""
    try:
        have = sec.latest_period_end(facts)
        now = utcnow()
        filed = [x["period_end"] for x in sec.reports(app.session, cik, ua, now - timedelta(days=200), now)]
        newest = max(filed, default=None)
        if have and newest and newest > have:
            facts = sec.company_facts(app.session, cik, ua, fresh=True)
            have2 = sec.latest_period_end(facts)
            if have2 and have2 < newest:
                note = f"XBRL 数据最新只到 {have2}，但 SEC 已有截至 {newest} 的 10-K/10-Q（结构化数据尚未更新，该期数字请查申报原文 documents）"
    except Exception:  # 校验是附加提示，失败不影响数据本身
        pass
    return facts, note


def _tax(facts: dict):
    tax = sec._taxonomy(facts)
    if not tax:
        return None
    name, gaap, _ = tax
    cur = sec._currency(gaap)
    return (name, gaap, cur, CONCEPTS[name]) if cur else None


def _first_at(gaap: dict, concepts: list[str], unit: str, end: str) -> tuple[str, float] | None:
    """某时点第一个有数据的概念：(概念名, 值)。"""
    for c in concepts:
        v = sec.merged_values(gaap, [c], unit, instant=True).get((None, end))
        if v is not None:
            return c, v
    return None


def fcf_data(facts: dict) -> dict | None:
    """emdata.fcf_data 同形状：flows（报告期倒序；10-Q 为年初至今累计）与最新资产负债表。
    资本开支 = 购建物业厂房设备 + 资本化软件；有息负债 = 短期借款 + 一年内到期债务 + 长期债务（经营租赁负债不计入）。"""
    t = _tax(facts)
    if not t:
        return None
    _, gaap, cur, c = t
    ocf = sec.merged_values(gaap, c["ocf"], cur)
    end = sec.latest_period_end(facts)
    if not ocf or not end:
        return None
    capex = sec.merged_values(gaap, c["capex"], cur)
    soft = sec.merged_values(gaap, c["software"], cur) if c["software"] else {}
    inv = sec.merged_values(gaap, c["inv"], cur)
    by_end: dict[str, tuple] = {}
    for (s, e), v in ocf.items():
        days = sec._days(s, e)
        if days >= 80 and (e not in by_end or days > by_end[e][0]):
            by_end[e] = (days, s, v)
    flows = [{"date": e, "annual": 350 <= d <= 380, "ocf": v,
              "capex": abs(capex.get((s, e), 0.0)) + abs(soft.get((s, e), 0.0)), "inv": inv.get((s, e))}
             for e, (d, s, v) in by_end.items()]
    items: dict[str, float] = {}

    def pick(label: str, key: str) -> None:
        got = _first_at(gaap, c[key], cur, end) if c[key] else None
        if got and got[1]:
            items[label] = got[1]

    pick("短期借款", "debt_short"), pick("一年内到期债务", "debt_cur"), pick("长期债务", "debt_long")
    if not items.get("长期债务") and not items.get("一年内到期债务"):  # 只披露合计时用合计
        items.pop("长期债务", None)
        pick("债务合计", "debt_total")
    cash = (_first_at(gaap, c["cash"], cur, end) or (None, 0.0))[1]
    inv_ = (_first_at(gaap, c["invest"], cur, end) or (None, 0.0))[1]
    restricted = sum(v for k in c["restricted"][:1] if (v := (_first_at(gaap, [k], cur, end) or (None, 0.0))[1]))
    minority = (_first_at(gaap, c["minority"], cur, end) or (None, 0.0))[1]
    liabilities = (_first_at(gaap, c["liabilities"], cur, end) or (None, None))[1]
    balance = {"date": end, "cash": cash, "cash_items": {"现金及现金等价物": cash} if cash else {}, "invest": inv_,
               "debt": sum(items.values()), "debt_items": items, "minority": minority, "liabilities": liabilities,
               "restricted": restricted}
    return {"flows": sorted(flows, key=lambda x: x["date"], reverse=True), "balance": balance, "currency": cur}


def _clean_eps(eps: dict, gaap: dict, cur: str, c: dict) -> dict:
    """剔除标错数量级的每股收益（BE 的 10-Q 把 -0.10 标成 -100）：与「净利润 ÷ 加权股数」相差 20 倍以上的丢掉。"""
    ni = sec.merged_values(gaap, c["ni"], cur)
    sh = sec.merged_values(gaap, c["wshares"], "shares")
    out = {}
    for k, v in eps.items():
        n, s = ni.get(k), sh.get(k)
        if n and s and v:
            ratio = abs(v / (n / s))
            if ratio > 20 or ratio < 0.05:
                continue
        out[k] = v
    return out


def eps_bps_points(facts: dict) -> tuple[list[tuple[str, float | None, float | None]], str | None]:
    """估值序列用的 [(可得日期, EPS(TTM), 每股净资产)]（季报晚 45 天、年报晚 75 天视为可得）与报告币种。"""
    t = _tax(facts)
    if not t:
        return [], None
    _, gaap, cur, c = t
    eps = _clean_eps(sec.merged_values(gaap, c["eps"], f"{cur}/shares"), gaap, cur, c)
    equity = sec.merged_values(gaap, c["equity"], cur, instant=True)
    shares = sec.merged_values(gaap, c["shares_out"], "shares", instant=True)
    weighted = sec.merged_values(gaap, c["wshares"], "shares")
    wend: dict[str, float] = {}  # 每个截止日取最短一段的加权股数（期末股数缺失时的备选）
    for (s, e), v in sorted(weighted.items(), key=lambda kv: -sec._days(*kv[0])):
        wend[e] = v
    annual_ends = {e for (s, e) in eps if 350 <= sec._days(s, e) <= 380}
    points = []
    for end in sorted({e for _, e in eps}):
        ttm = sec.ttm_at(eps, end)
        eq, sh = equity.get((None, end)), shares.get((None, end)) or wend.get(end)
        bps = eq / sh if eq is not None and sh else None
        lag = 75 if end in annual_ends else 45
        points.append(((date.fromisoformat(end) + timedelta(days=lag)).isoformat(), ttm, bps))
    return points, cur


# ---------------------------------------------------------------- 主要财务指标

def indicators_table(facts: dict, listing: Listing, periods: int) -> str | None:
    """单季（含推导的 Q4）、全年与近 12 个月的主要指标：收入及同比、毛利率、经营利润率、净利润及净利率、EPS、资产负债率、流动比率、ROE。"""
    inc = sec.statement_from_facts(facts, "income", periods + 5)
    bal = sec.statement_from_facts(facts, "balance", periods + 8)
    if not inc:
        return None
    rows = dict(inc["rows"])
    brows = dict(bal["rows"]) if bal else {}
    bdates = [p["date"] for p in bal["periods"]] if bal else []

    def col(table: dict, label: str, i: int):
        return (table.get(label) or [None] * 99)[i]

    def bal_at(label: str, d: str):
        return col(brows, label, bdates.index(d)) if d in bdates else None

    def ratio(a, b):
        return a / b * 100 if a is not None and b else None

    def prior(i: int):
        """去年同期（同样长度、相隔约一年、同为 TTM 或同为非 TTM）的列号。"""
        c = inc["periods"][i]
        for j, p in enumerate(inc["periods"]):
            if (j != i and bool(p.get("ttm")) == bool(c.get("ttm")) and p["days"] and c["days"] and abs(p["days"] - c["days"]) <= 10
                    and 355 <= (date.fromisoformat(c["date"]) - date.fromisoformat(p["date"])).days <= 375):
                return j
        return None

    out = []
    for i, p in enumerate(inc["periods"][:periods + 1]):
        rev, gp, op, ni = (col(rows, k, i) for k in ("收入", "毛利", "经营利润", "净利润"))
        eps = col(rows, "稀释每股收益", i)
        j = prior(i)
        yoy = (rev / col(rows, "收入", j) - 1) * 100 if j is not None and rev is not None and col(rows, "收入", j) else None
        a, l_, ca, cl, eq = (bal_at(k, p["date"]) for k in ("总资产", "总负债", "流动资产合计", "流动负债合计", "股东权益（归属母公司）"))
        full = p["days"] and p["days"] >= 350 or p.get("ttm")
        out.append({"label": p["label"], "rev": rev, "yoy": yoy, "gm": ratio(gp, rev), "om": ratio(op, rev), "ni": ni, "nm": ratio(ni, rev),
                    "eps": eps, "debt_ratio": ratio(l_, a), "current": ca / cl if ca is not None and cl else None,
                    "roe": ratio(ni, eq) if full else None})
    cur = inc["currency"]

    def f(v, d=2, scale=1.0):
        return "—" if v is None else f"{v / scale:,.{d}f}"

    lines = [f"{listing.display} 主要财务指标（SEC XBRL 派生，程序计算；收入与净利润单位：亿 {cur}；"
             "单季来自 10-Q（Q4 与近 12 个月为推导）；ROE = 全年/近12个月净利润 ÷ 期末归母净资产）：",
             "| 期间 | 收入 | 同比% | 毛利率% | 经营利润率% | 净利润 | 净利率% | 稀释EPS | 资产负债率% | 流动比率 | ROE% |", "|" + "---|" * 11]
    for r in out:
        lines.append(f"| {r['label']} | {f(r['rev'], 2, 1e8)} | {f(r['yoy'], 1)} | {f(r['gm'], 1)} | {f(r['om'], 1)} | {f(r['ni'], 2, 1e8)} | "
                     f"{f(r['nm'], 1)} | {f(r['eps'])} | {f(r['debt_ratio'], 1)} | {f(r['current'])} | {f(r['roe'], 1)} |")
    return "\n".join(lines)


# ---------------------------------------------------------------- 股东、内部人、做空

def _mm(v: float | None) -> str:
    return "—" if v is None else f"{v / 1e6:+,.2f} 百万股".replace("+", "") if v >= 0 else f"{v / 1e6:,.2f} 百万股"


def holders_text(app: App, listing: Listing) -> str:
    """机构持仓（Nasdaq 汇总的 13F）、内部人交易（Form 4）、5% 以上大股东申报（SEC 13D/13G 列表）。"""
    out = []
    inst = _try(app, "nasdaq", nasdaq.institutions, app.session, listing.code)
    if inst:
        out.append(f"{listing.display} 机构持仓（Nasdaq 汇总 13F；各机构报告期不同，最新一季披露期内可能不全）：机构合计持有已发行股份约 {num(inst['pct'])}%"
                   f"（{num(inst['holders'], 0)} 家，{_mm(inst['shares'])}）；上季增持 {num(inst['increased'], 0)} 家 / 减持 {num(inst['decreased'], 0)} 家，"
                   f"新进 {num(inst['new'], 0)} 家 / 清仓 {num(inst['sold_out'], 0)} 家。前几大持有人：")
        out += [f"- {h['name']}｜{h['date']}｜{_mm(h['shares'])}｜较上期 {_mm(h['change'])}"
                + (f"（{h['change_pct']:+.1f}%）" if h["change_pct"] is not None else "") for h in inst["top"][:10]]
    ins = _try(app, "nasdaq", nasdaq.insiders, app.session, listing.code)
    if ins:
        out.append(f"\n{listing.display} 内部人交易（Form 4，Nasdaq 汇总）：")
        out += [f"- {a}：近 3 个月 {b}，近 12 个月 {c}" for a, b, c in ins["summary"]]
        out.append("最近的申报（「Automatic Sell」多为 10b5-1 计划的预设卖出，信号弱于主动的 Open Market Sell / Buy）：")
        out += [f"- {r['date']}｜{r['name']}（{r['relation']}）｜{r['type']}｜{num(r['shares'], 0)} 股 @ {num(r['price'])}｜交易后持有 {num(r['held'], 0)}"
                for r in ins["recent"][:12]]
    big = _big_holder_filings(app, listing)
    if big:
        out.append(f"\n{listing.display} 近期 5% 以上大股东申报（SEC 13D/13G，只列申报日与链接，持有人与比例请用 fetch_url 打开）：")
        out += big
    return "\n".join(out) or f"{listing.display}：没有取到股东数据（Nasdaq 与 SEC 都无返回）"


BIG_FORMS = {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A"}


def _big_holder_filings(app: App, listing: Listing, limit: int = 8) -> list[str]:
    ua = app.settings.sources.sec_user_agent
    if not ua:
        return []
    cik = ids.sec_cik(app.db, app.session, listing, ua)
    if not cik:
        return []
    rows = _try(app, "sec", sec.filings, app.session, cik, ua, utcnow() - timedelta(days=540), BIG_FORMS) or []
    rows = sorted(rows, key=lambda x: x["published_at"], reverse=True)[:limit]
    return [f"- {r['published_at'].date()}｜{r['category']}｜{r['url']}" for r in rows]


def short_text(app: App, listing: Listing) -> str:
    """做空持仓（Nasdaq 上市用 Nasdaq，其余用 FINRA 双周文件）与最近的每日做空成交量占比。"""
    sym = listing.code
    rows: list[dict] = []
    src = "Nasdaq"
    if (listing.exchange or "").upper() == "NASDAQ":
        rows = _try(app, "nasdaq", nasdaq.short_interest, app.session, sym) or []
    if not rows:
        src = "FINRA"
        for d in finra.settlement_dates(today_local(), 6):
            key = f"finra_si:{d.isoformat()}:{sym}"
            cached = app.db.kv_get(key)
            if cached:
                r = json.loads(cached)
            else:
                r = _try(app, "finra", finra.short_interest_file, app.session, d, sym)
                if r is False:  # 该结算日的文件还没发布
                    continue
                if r:
                    app.db.kv_set(key, json.dumps(r))
            if r:
                rows.append({"date": r["date"], "short": r["short"], "avg_volume": r["avg_volume"], "days_to_cover": r["days_to_cover"]})
            if len(rows) >= 4:
                break
    lines = []
    if rows:
        q = app.db.quote(app.secid(listing)) or {}
        shares = q.get("shares") or (q["market_cap"] / q["price"] if q.get("market_cap") and q.get("price") else None)
        lines.append(f"{listing.display} 做空持仓（{src}，双周结算；占比按已知股本估算）：")
        for r in rows[:4]:
            lines.append(f"- {r['date']}｜做空 {num(r['short'] / 1e6)} 百万股" + (f"（{pct(r['short'] / shares, sign=False)} 的股本）" if shares else "")
                         + f"｜日均成交 {num(r['avg_volume'] / 1e6)} 百万股｜回补天数 {num(r['days_to_cover'])}")
        if len(rows) >= 2 and rows[0]["short"] and rows[-1]["short"]:
            lines.append(f"- 区间变化：{rows[-1]['date']} → {rows[0]['date']} 做空股数 {pct(rows[0]['short'] / rows[-1]['short'] - 1)}")
    else:
        lines.append(f"{listing.display}：没有取到做空持仓（{src} 暂无数据）")
    vol = []
    d = today_local()
    for _ in range(14):
        d -= timedelta(days=1)
        if d.weekday() >= 5:
            continue
        r = _try(app, "finra", finra.daily_short_volume, app.session, d, sym)
        if r:
            vol.append(r)
        if len(vol) >= 5:
            break
    if vol:
        avg = sum(r["ratio"] for r in vol if r["ratio"]) / max(1, len([r for r in vol if r["ratio"]]))
        lines.append("最近每日卖空成交量占比（FINRA RegSHO，含做市商对冲，不等于净做空；45% 上下是常态）："
                     + "，".join(f"{r['date'][4:]} {pct(r['ratio'], sign=False)}" for r in vol) + f"；均值 {pct(avg, sign=False)}")
    return "\n".join(lines)
