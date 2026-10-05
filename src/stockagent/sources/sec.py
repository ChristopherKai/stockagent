"""美国 SEC EDGAR：8-K / 10-Q / 10-K 等申报文件，以及 XBRL 结构化财务数据（companyfacts）。SEC 要求 User-Agent 带联系方式。"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone

import requests

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"


def lookup_cik(session: requests.Session, ticker: str, user_agent: str) -> int | None:
    r = session.get(TICKERS_URL, headers={"User-Agent": user_agent}, timeout=30)
    r.raise_for_status()
    t = ticker.upper()
    for row in (r.json() or {}).values():
        if row.get("ticker", "").upper() == t:
            return int(row["cik_str"])
    return None


ITEM_LABELS = {
    "1.01": "重大协议", "1.02": "协议终止", "2.01": "资产收购/处置完成", "2.02": "业绩公告", "2.03": "新增重大债务",
    "2.05": "重组/退出成本", "2.06": "重大减值", "3.01": "退市/上市标准通知", "3.02": "未注册股权发行",
    "4.01": "更换审计师", "4.02": "财报不可依赖（重述）", "5.01": "控制权变更", "5.02": "董事/高管变动",
    "5.03": "章程修订", "5.07": "股东大会表决", "7.01": "披露（Reg FD）", "8.01": "其他重要事项", "9.01": "财务报表与附件",
}
_EXHIBIT = re.compile(r"ex[-_]?99", re.I)  # 新闻稿 / 业绩公告通常是 EX-99.1，文件名形如 ex991_*.htm、d123dex991.htm
_PAGE_LINE = re.compile(r"\[第\d+页\]")
_BOILERPLATE = re.compile(r"take no responsibility|stock code|incorporated in the|股份代[號号]|不對.*負責", re.I)  # 港交所公告的免责声明与股票代码行
_EXHIBIT_JUNK = re.compile(r"EX-99(\.\d+)?|\d+|\S+\.html?|Exhibit 99(\.\d+)?", re.I)
EXHIBIT_FORMS = {"8-K", "6-K"}


def exhibit_text(session: requests.Session, cik: int, accession: str, user_agent: str, max_chars: int = 2000, with_full: bool = False):
    """8-K / 6-K 的 EX-99.1（新闻稿、业绩公告）正文开头：(首行标题, 正文前 max_chars 字)；没有附件返回 None。
    with_full 时多返回全文与附件链接：(标题, 开头, 全文, 链接)。"""
    from ..htmltext import html_text

    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}"
    r = session.get(f"{base}/index.json", headers={"User-Agent": user_agent}, timeout=30)
    r.raise_for_status()
    names = sorted(i["name"] for i in (r.json() or {}).get("directory", {}).get("item", [])
                   if _EXHIBIT.search(i["name"]) and i["name"].lower().endswith((".htm", ".html", ".txt")))
    if not names:
        return None
    r = session.get(f"{base}/{names[0]}", headers={"User-Agent": user_agent}, timeout=60)
    r.raise_for_status()
    text = html_text(r.text)[0] if names[0].lower().endswith((".htm", ".html")) else r.text
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not _PAGE_LINE.fullmatch(ln.strip()) and not _BOILERPLATE.search(ln)]
    while lines and _EXHIBIT_JUNK.fullmatch(lines[0]):  # 附件文件开头的「EX-99.1 / 2 / 文件名 / Exhibit 99.1」标签行
        lines.pop(0)
    if not lines:
        return None
    body = "\n".join(lines)
    head = next((ln for ln in lines if len(ln) >= 20 and not ln.isupper() and not ln[0].islower()), lines[0])[:120]  # 跳过「PRESS RELEASE」之类的栏目行
    return (head, body[:max_chars], body, f"{base}/{names[0]}") if with_full else (head, body[:max_chars])


def filings(session: requests.Session, cik: int, user_agent: str, start: datetime, forms: set[str]) -> list[dict]:
    r = session.get(SUBMISSIONS_URL.format(cik=cik), headers={"User-Agent": user_agent}, timeout=30)
    r.raise_for_status()
    recent = (r.json() or {}).get("filings", {}).get("recent", {})
    out = []
    for i, form in enumerate(recent.get("form", [])):
        if form not in forms:
            continue
        accepted = recent["acceptanceDateTime"][i]
        published = datetime.fromisoformat(accepted.replace("Z", "+00:00")).astimezone(timezone.utc)
        if published < start:
            continue
        acc = recent["accessionNumber"][i]
        doc = recent["primaryDocument"][i]
        desc = recent.get("primaryDocDescription", [""] * (i + 1))[i] or ""
        items = recent.get("items", [""] * (i + 1))[i] or ""
        labels = "、".join(ITEM_LABELS[i] for i in (x.strip() for x in items.split(",")) if i in ITEM_LABELS and i != "9.01")
        title = f"{form} {desc}".strip() + (f"（8-K items: {items}" + (f" · {labels}" if labels else "") + "）" if items else "")
        out.append({
            "source_id": acc,
            "title": title,
            "category": form,
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}",
            "published_at": published,
        })
    return out


def reports(session: requests.Session, cik: int, user_agent: str, start: datetime, end: datetime) -> list[dict]:
    """年报（10-K / 20-F / 40-F）与季报（10-Q），按申报时间倒序；doc_type 与港股、A 股的口径一致。
    period 取报告期截止日所在年份（年报）或 YYYY-MM-DD（季报，财年不一定是日历年，不硬套 Q1–Q4）。"""
    r = session.get(SUBMISSIONS_URL.format(cik=cik), headers={"User-Agent": user_agent}, timeout=30)
    r.raise_for_status()
    recent = (r.json() or {}).get("filings", {}).get("recent", {})
    out = []
    for i, form in enumerate(recent.get("form", [])):
        doc_type = ("annual_report" if form in ("10-K", "20-F", "40-F") else "quarterly_report" if form == "10-Q"
                    else "other" if form == "DEF 14A" else None)  # 代理声明（股东大会材料）：高管薪酬、股权激励、持股 5% 以上股东
        if doc_type is None:
            continue
        published = datetime.fromisoformat(recent["acceptanceDateTime"][i].replace("Z", "+00:00")).astimezone(timezone.utc)
        if not start <= published <= end:
            continue
        acc, doc = recent["accessionNumber"][i], recent["primaryDocument"][i]
        period_end = (recent.get("reportDate") or [""] * (i + 1))[i] or published.date().isoformat()
        out.append({
            "doc_type": doc_type,
            "title": f"{form} {period_end}" if doc_type != "other" else f"{form}（{published.date()} 申报）",
            "period": period_end[:4] if doc_type == "annual_report" else f"DEF14A-{published.year}" if doc_type == "other" else period_end,
            "period_end": period_end,
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}",
            "published_at": published,
        })
    return sorted(out, key=lambda x: x["published_at"], reverse=True)


# ---------------------------------------------------------------- XBRL 财务数据（companyfacts）

FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
REPORT_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
# (中文科目, us-gaap 概念)；公司各自选用的概念不同（也会换用），逐期取第一个有数据的
US_ITEMS = {
    "income": [
        ("收入", ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
                  "RevenueFromContractWithCustomerIncludingAssessedTax", "RevenuesExcludingInterestAndDividends"]),
        ("营业成本", ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"]),
        ("毛利", ["GrossProfit"]),
        ("研发费用", ["ResearchAndDevelopmentExpense"]),
        ("销售、一般及管理费用", ["SellingGeneralAndAdministrativeExpense"]),
        ("销售费用", ["SellingAndMarketingExpense"]),
        ("管理费用", ["GeneralAndAdministrativeExpense"]),
        ("经营利润", ["OperatingIncomeLoss"]),
        ("利息费用", ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"]),
        ("税前利润", ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                      "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"]),
        ("所得税", ["IncomeTaxExpenseBenefit"]),
        ("净利润", ["NetIncomeLoss", "ProfitLoss"]),
        ("归属普通股股东的净利润", ["NetIncomeLossAvailableToCommonStockholdersBasic"]),
        ("基本每股收益", ["EarningsPerShareBasic", "EarningsPerShareBasicAndDiluted"]),
        ("稀释每股收益", ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"]),
        ("加权平均基本股数（亿股）", ["WeightedAverageNumberOfSharesOutstandingBasic"]),
        ("加权平均稀释股数（亿股）", ["WeightedAverageNumberOfDilutedSharesOutstanding"]),
    ],
    "balance": [
        ("现金及现金等价物", ["CashAndCashEquivalentsAtCarryingValue"]),
        ("短期投资", ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"]),
        ("应收账款", ["AccountsReceivableNetCurrent"]),
        ("存货", ["InventoryNet"]),
        ("流动资产合计", ["AssetsCurrent"]),
        ("物业、厂房及设备净值", ["PropertyPlantAndEquipmentNet"]),
        ("经营租赁使用权资产", ["OperatingLeaseRightOfUseAsset"]),
        ("商誉", ["Goodwill"]),
        ("无形资产（不含商誉）", ["IntangibleAssetsNetExcludingGoodwill"]),
        ("总资产", ["Assets"]),
        ("应付账款", ["AccountsPayableCurrent"]),
        ("合同负债/递延收入（流动）", ["ContractWithCustomerLiabilityCurrent", "DeferredRevenueCurrent"]),
        ("短期借款及一年内到期的长期借款", ["DebtCurrent", "ShortTermBorrowings", "LongTermDebtCurrent", "ConvertibleNotesPayableCurrent"]),
        ("长期借款", ["LongTermDebtNoncurrent", "LongTermDebt", "ConvertibleNotesPayableNoncurrent", "ConvertibleDebtNoncurrent",
                      "LongTermNotesPayable"]),
        ("流动负债合计", ["LiabilitiesCurrent"]),
        ("总负债", ["Liabilities"]),
        ("股东权益（归属母公司）", ["StockholdersEquity"]),
        ("少数股东权益", ["MinorityInterest"]),
        ("股东权益合计", ["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]),
    ],
    "cashflow": [
        ("经营活动现金流量净额", ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]),
        ("购建物业、厂房及设备", ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
                                  "PaymentsToAcquireOtherPropertyPlantAndEquipment"]),
        ("资本化软件/无形资产支出", ["PaymentsForSoftware", "PaymentsToAcquireIntangibleAssets"]),
        ("投资活动现金流量净额", ["NetCashProvidedByUsedInInvestingActivities", "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations"]),
        ("融资活动现金流量净额", ["NetCashProvidedByUsedInFinancingActivities", "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations"]),
        ("折旧与摊销", ["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet", "Depreciation"]),
        ("股权激励费用", ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"]),
        ("回购股份支付的现金", ["PaymentsForRepurchaseOfCommonStock", "PaymentsForRepurchaseOfEquity"]),
        ("支付的股息", ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"]),
    ],
}
# 按 IFRS 申报的外国发行人（ifrs-full 概念）；科目名与上表一致
IFRS_ITEMS = {
    "income": [
        ("收入", ["Revenue", "RevenueFromContractsWithCustomers"]),
        ("营业成本", ["CostOfSales"]),
        ("毛利", ["GrossProfit"]),
        ("研发费用", ["ResearchAndDevelopmentExpense"]),
        ("销售费用", ["SellingExpense", "DistributionCosts"]),
        ("管理费用", ["AdministrativeExpense"]),
        ("经营利润", ["ProfitLossFromOperatingActivities"]),
        ("利息费用", ["InterestExpense", "FinanceCosts"]),
        ("税前利润", ["ProfitLossBeforeTax"]),
        ("所得税", ["IncomeTaxExpenseContinuingOperations"]),
        ("净利润", ["ProfitLoss"]),
        ("归属普通股股东的净利润", ["ProfitLossAttributableToOwnersOfParent"]),
        ("基本每股收益", ["BasicEarningsLossPerShare"]),
        ("稀释每股收益", ["DilutedEarningsLossPerShare"]),
        ("加权平均基本股数（亿股）", ["WeightedAverageShares"]),
    ],
    "balance": [
        ("现金及现金等价物", ["CashAndCashEquivalents"]),
        ("应收账款", ["TradeAndOtherCurrentReceivables", "CurrentTradeReceivables"]),
        ("存货", ["Inventories"]),
        ("流动资产合计", ["CurrentAssets"]),
        ("物业、厂房及设备净值", ["PropertyPlantAndEquipment"]),
        ("商誉", ["Goodwill"]),
        ("总资产", ["Assets"]),
        ("应付账款", ["TradeAndOtherCurrentPayables", "CurrentTradePayables"]),
        ("流动负债合计", ["CurrentLiabilities"]),
        ("总负债", ["Liabilities"]),
        ("股东权益（归属母公司）", ["EquityAttributableToOwnersOfParent"]),
        ("少数股东权益", ["NoncontrollingInterests"]),
        ("股东权益合计", ["Equity"]),
    ],
    "cashflow": [
        ("经营活动现金流量净额", ["CashFlowsFromUsedInOperatingActivities"]),
        ("购建物业、厂房及设备", ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"]),
        ("投资活动现金流量净额", ["CashFlowsFromUsedInInvestingActivities"]),
        ("融资活动现金流量净额", ["CashFlowsFromUsedInFinancingActivities"]),
        ("折旧与摊销", ["DepreciationAndAmortisationExpense"]),
        ("股权激励费用", ["AdjustmentsForSharebasedPayments"]),
        ("支付的股息", ["DividendsPaidClassifiedAsFinancingActivities"]),
    ],
}
# 每张表必须有的科目：缺了说明公司用了未收录的概念，表尾提示去看申报原文
CORE_ITEMS = {"income": ["收入", "净利润"], "balance": ["总资产", "总负债", "现金及现金等价物"],
              "cashflow": ["经营活动现金流量净额", "购建物业、厂房及设备"]}
# 公司常把成本拆成几条分别标记（如 CostOfGoodsAndServicesSold 只是其中一条小项），总成本取同期各概念中最大的
PICK_MAX = {"营业成本"}
SHARE_ITEMS = {"加权平均基本股数（亿股）", "加权平均稀释股数（亿股）"}  # 单位是股，不是货币

_facts_cache: dict[int, tuple[float, dict]] = {}
FACTS_TTL = 3600  # 同一进程内复用（深度报告的 4 个研究 agent 会各查一次，文件有几 MB）


def company_facts(session: requests.Session, cik: int, user_agent: str, fresh: bool = False) -> dict:
    hit = _facts_cache.get(cik)
    if hit and not fresh and time.time() - hit[0] < FACTS_TTL:
        return hit[1]
    r = session.get(FACTS_URL.format(cik=cik), headers={"User-Agent": user_agent}, timeout=60)
    r.raise_for_status()
    data = r.json() or {}
    _facts_cache[cik] = (time.time(), data)
    return data


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days + 1


def _taxonomy(facts: dict) -> tuple[str, dict, dict] | None:
    """(分类标准名, 概念字典, 科目表)：优先 us-gaap；只有 ifrs-full 的发行人用 IFRS 科目表。"""
    all_facts = facts.get("facts") or {}
    for name, table in (("us-gaap", US_ITEMS), ("ifrs-full", IFRS_ITEMS)):
        if all_facts.get(name):
            return name, all_facts[name], table
    return None


def _currency(gaap: dict) -> str | None:
    """报表货币：收入或总资产等概念最常用的货币单位（中概股的 20-F 多为 CNY）。"""
    for concept in ("Assets", "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "Revenue", "NetIncomeLoss", "ProfitLoss"):
        units = (gaap.get(concept) or {}).get("units") or {}
        units = {u: v for u, v in units.items() if "/" not in u and u != "shares"}
        if units:
            return max(units, key=lambda u: len(units[u]))
    return None


def _concept_values(gaap: dict, concept: str, unit: str) -> dict[tuple, float]:
    """某概念在定期报告中的取值：{(start 或 None, end): 值}，同一期间取最后一次申报（重述后的数字）。"""
    out: dict[tuple, tuple[str, float]] = {}
    for f in ((gaap.get(concept) or {}).get("units") or {}).get(unit) or []:
        if f.get("form") not in REPORT_FORMS or f.get("val") is None:
            continue
        key = (f.get("start"), f["end"])
        if key not in out or f.get("filed", "") >= out[key][0]:
            out[key] = (f.get("filed", ""), float(f["val"]))
    return {k: v for k, (_, v) in out.items()}


def _shift(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def _near(a: str, b: str, tol: int = 7) -> bool:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days) <= tol


def derive_flows(vals: dict[tuple, float]) -> tuple[dict[tuple, float], float | None, str | None]:
    """流量科目的推导：(第四季度单季 {(起, 止): 值}, 近 12 个月 TTM, TTM 截止日)。
    Q4 = 全年 − 同一财年前 9 个月累计；TTM = 上一财年全年 + 本年累计 − 去年同期累计（最新期本身是全年时无需推导）。"""
    full = {k: v for k, v in vals.items() if 350 <= _days(*k) <= 380}
    q4: dict[tuple, float] = {}
    for (s, e), v in full.items():
        for (s2, e2), v2 in vals.items():
            if s2 == s and 260 <= _days(s2, e2) <= 285:
                q4[(_shift(e2, 1), e)] = v - v2
    ttm, ttm_end = None, None
    ytd = {k: v for k, v in vals.items() if 80 <= _days(*k) < 350}
    if ytd:
        latest = max(ytd, key=lambda k: (k[1], _days(*k)))
        s, e = latest
        if not any(k[1] >= e for k in full):  # 最新一期比最近的全年还新
            prev_full = next((k for k in full if _near(k[1], _shift(s, -1), 3)), None)
            prior_ytd = next((k for k in ytd if prev_full and k[0] == prev_full[0] and _near(k[1], _shift(e, -365), 7)), None)
            if prev_full and prior_ytd:
                ttm, ttm_end = full[prev_full] + ytd[latest] - ytd[prior_ytd], e
    return q4, ttm, ttm_end


def _drop_bad_eps(rows: list[tuple[str, dict]]) -> list[str]:
    """申报里偶有每股收益标错数量级（BE 的 10-Q 把 -0.10 标成 -100）：与「净利润 ÷ 加权股数」相差 20 倍以上的剔除并提示。"""
    by = dict(rows)
    ni_common, ni_all = by.get("归属普通股股东的净利润") or {}, by.get("净利润") or {}
    notes = []
    for eps_label, shares_label in (("基本每股收益", "加权平均基本股数（亿股）"), ("稀释每股收益", "加权平均稀释股数（亿股）")):
        eps, shares = by.get(eps_label) or {}, by.get(shares_label) or {}
        for k in list(eps):
            n, sh = ni_common.get(k) or ni_all.get(k), shares.get(k)
            if not n or not sh or not eps[k]:
                continue
            ratio = abs(eps[k] / (n / sh))
            if ratio > 20 or ratio < 0.05:
                notes.append(f"{k[1]} {eps_label}申报值 {eps[k]:g} 与净利润÷加权股数（{n / sh:.2f}）相差过大，疑似申报标错，已剔除")
                del eps[k]
    return notes


def _period_label(start: str | None, end: str, derived: bool = False) -> str:
    if start is None:
        return end
    days = _days(start, end)
    span = "全年" if days >= 350 else f"{round(days / 30.44)}个月"
    return f"{end}（{span}{'·推导' if derived else ''}）"


def statement_from_facts(facts: dict, kind: str, periods: int = 6, annual_only: bool = False) -> dict | None:
    """companyfacts → {"currency", "periods": [{"date", "label"}], "rows": [(科目, [各期金额])], "missing": [缺失的核心科目]}。
    流量表每个截止日取最短的一段（10-Q 的利润表给 3 个月，现金流量表只有年初至今累计），并另列全年；
    第四季度单季 = 全年 − 前 9 个月累计、近 12 个月 TTM 由代码推导（标「推导」）；
    annual_only 时只取约 12 个月的期间（资产负债表取年报中出现的时点）。只有 ifrs-full 概念的发行人用 IFRS 科目表。"""
    tax = _taxonomy(facts)
    if not tax:
        return None
    _, gaap, table = tax
    cur = _currency(gaap)
    if not cur:
        return None
    instant = kind == "balance"
    rows: list[tuple[str, dict]] = []
    derived_keys: set[tuple] = set()
    ttm_by_label: dict[str, float] = {}
    ttm_end: str | None = None
    for label, concepts in table[kind]:
        unit = f"{cur}/shares" if "每股" in label else "shares" if label in SHARE_ITEMS else cur
        merged: dict[tuple, float] = {}
        for c in reversed(concepts):  # 公司换用过概念时逐期合并，靠前的概念优先
            got = {k: v for k, v in _concept_values(gaap, c, unit).items() if (k[0] is None) == instant}
            if label in PICK_MAX:
                got = {k: max(v, merged.get(k, v)) for k, v in got.items()}
                merged = {**merged, **got}
            else:
                merged.update(got)
        if merged and not instant and "每股" not in label and label not in SHARE_ITEMS:
            q4, ttm, end = derive_flows(merged)
            for k, v in q4.items():
                if k not in merged:
                    merged[k] = v
                    derived_keys.add(k)
            if ttm is not None:
                ttm_by_label[label], ttm_end = ttm, end
        if merged:
            rows.append((label, merged))
    if not rows:
        return None
    notes = _drop_bad_eps(rows) if kind == "income" else []
    anchor = rows[0][1]
    annual_ends = _annual_instants(gaap, cur) if instant and annual_only else None
    chosen: dict[str, tuple] = {}
    full_years: list[tuple] = []
    for start, end in anchor:
        if instant:
            if annual_ends is None or end in annual_ends:
                chosen[end] = (None, end)
            continue
        days = _days(start, end)
        if days < 80 or (annual_only and not 350 <= days <= 380):
            continue
        if days >= 350:
            full_years.append((start, end))
        prev = chosen.get(end)
        if prev is None or days < _days(prev[0], end):
            chosen[end] = (start, end)
    ends = sorted(chosen, reverse=True)[:periods]
    keys = []
    for e in ends:  # 推导出 Q4 后，同一截止日的全年数字另列一列
        keys.append(chosen[e])
        keys += [k for k in full_years if k[1] == e and k != chosen[e] and not annual_only]
    cols = [{"date": end, "label": _period_label(start, end, (start, end) in derived_keys),
             "days": _days(start, end) if start else None} for start, end in keys]
    out_rows = [(label, [vals.get(k) for k in keys]) for label, vals in rows]
    if ttm_by_label and not annual_only and ttm_end and ends and ttm_end >= ends[0]:
        cols.insert(0, {"date": ttm_end, "label": f"{ttm_end}（近12个月·推导）", "days": 365, "ttm": True})
        out_rows = [(label, [ttm_by_label.get(label), *values]) for label, values in out_rows]
    have = {label for label, values in out_rows if any(v is not None for v in values)}
    return {
        "currency": cur,
        "periods": cols,
        "rows": [(label, values) for label, values in out_rows if label in have],
        "missing": [c for c in CORE_ITEMS[kind] if c not in have],
        "notes": notes,
    }


def merged_values(gaap: dict, concepts: list[str], unit: str, instant: bool = False) -> dict[tuple, float]:
    """多个候选概念按「靠前优先」逐期合并：{(start 或 None, end): 值}（statement_from_facts 与 usdata 共用）。"""
    merged: dict[tuple, float] = {}
    for c in reversed(concepts):
        merged.update({k: v for k, v in _concept_values(gaap, c, unit).items() if (k[0] is None) == instant})
    return merged


def ttm_at(vals: dict[tuple, float], end: str) -> float | None:
    """截至 end 的近 12 个月值：end 是全年截止日时即全年值；否则 = 上一财年全年 + 本年累计 − 去年同期累计。"""
    full = {k: v for k, v in vals.items() if 350 <= _days(*k) <= 380}
    for k, v in full.items():
        if k[1] == end:
            return v
    ytd = [(k, v) for k, v in vals.items() if k[1] == end and 80 <= _days(*k) < 350]
    if not ytd:
        return None
    (s, e), cur = max(ytd, key=lambda kv: _days(*kv[0]))
    prev_full = next((k for k in full if _near(k[1], _shift(s, -1), 3)), None)
    prior = next((v for k, v in vals.items() if prev_full and k[0] == prev_full[0] and _near(k[1], _shift(e, -365), 7)
                  and abs(_days(*k) - _days(s, e)) <= 5), None)
    return None if prev_full is None or prior is None else full[prev_full] + cur - prior


def latest_period_end(facts: dict) -> str | None:
    """XBRL 里最新一份 10-K / 10-Q / 20-F 的报告期截止日（取总资产的时点）。"""
    tax = _taxonomy(facts)
    if not tax:
        return None
    gaap = tax[1]
    ends = [f["end"] for f in ((gaap.get("Assets") or {}).get("units") or {}).get(_currency(gaap) or "USD") or [] if f.get("form") in REPORT_FORMS]
    return max(ends, default=None)


def _annual_instants(gaap: dict, unit: str) -> set[str]:
    """年报（10-K / 20-F）中出现过的资产负债表日。"""
    concept = gaap.get("Assets") or {}
    return {f["end"] for f in (concept.get("units") or {}).get(unit) or [] if f.get("form") in ANNUAL_FORMS}
