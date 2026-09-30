"""美国 SEC EDGAR：8-K / 10-Q / 10-K 等申报文件，以及 XBRL 结构化财务数据（companyfacts）。SEC 要求 User-Agent 带联系方式。"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone

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
        title = f"{form} {desc}".strip() + (f"（8-K items: {items}）" if items else "")
        out.append({
            "source_id": acc,
            "title": title,
            "category": form,
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}",
            "published_at": published,
        })
    return out


# ---------------------------------------------------------------- XBRL 财务数据（companyfacts）

FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
REPORT_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
# (中文科目, us-gaap 概念)；公司各自选用的概念不同（也会换用），逐期取第一个有数据的
US_ITEMS = {
    "income": [
        ("收入", ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
                  "RevenueFromContractWithCustomerIncludingAssessedTax"]),
        ("营业成本", ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"]),
        ("毛利", ["GrossProfit"]),
        ("研发费用", ["ResearchAndDevelopmentExpense"]),
        ("销售、一般及管理费用", ["SellingGeneralAndAdministrativeExpense"]),
        ("销售费用", ["SellingAndMarketingExpense"]),
        ("管理费用", ["GeneralAndAdministrativeExpense"]),
        ("经营利润", ["OperatingIncomeLoss"]),
        ("利息费用", ["InterestExpense", "InterestExpenseNonoperating"]),
        ("税前利润", ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                      "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"]),
        ("所得税", ["IncomeTaxExpenseBenefit"]),
        ("净利润", ["NetIncomeLoss", "ProfitLoss"]),
        ("归属普通股股东的净利润", ["NetIncomeLossAvailableToCommonStockholdersBasic"]),
        ("基本每股收益", ["EarningsPerShareBasic", "EarningsPerShareBasicAndDiluted"]),
        ("稀释每股收益", ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"]),
    ],
    "balance": [
        ("现金及现金等价物", ["CashAndCashEquivalentsAtCarryingValue"]),
        ("短期投资", ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"]),
        ("应收账款", ["AccountsReceivableNetCurrent"]),
        ("存货", ["InventoryNet"]),
        ("流动资产合计", ["AssetsCurrent"]),
        ("物业、厂房及设备净值", ["PropertyPlantAndEquipmentNet"]),
        ("商誉", ["Goodwill"]),
        ("总资产", ["Assets"]),
        ("应付账款", ["AccountsPayableCurrent"]),
        ("短期借款及一年内到期的长期借款", ["DebtCurrent", "ShortTermBorrowings", "LongTermDebtCurrent"]),
        ("长期借款", ["LongTermDebtNoncurrent", "LongTermDebt"]),
        ("流动负债合计", ["LiabilitiesCurrent"]),
        ("总负债", ["Liabilities"]),
        ("股东权益（归属母公司）", ["StockholdersEquity"]),
        ("少数股东权益", ["MinorityInterest"]),
        ("股东权益合计", ["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]),
    ],
    "cashflow": [
        ("经营活动现金流量净额", ["NetCashProvidedByUsedInOperatingActivities"]),
        ("购建物业、厂房及设备", ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"]),
        ("投资活动现金流量净额", ["NetCashProvidedByUsedInInvestingActivities"]),
        ("融资活动现金流量净额", ["NetCashProvidedByUsedInFinancingActivities"]),
        ("折旧与摊销", ["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet", "Depreciation"]),
        ("股权激励费用", ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"]),
        ("回购股份支付的现金", ["PaymentsForRepurchaseOfCommonStock"]),
        ("支付的股息", ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"]),
    ],
}

_facts_cache: dict[int, tuple[float, dict]] = {}
FACTS_TTL = 3600  # 同一进程内复用（深度报告的 4 个研究 agent 会各查一次，文件有几 MB）


def company_facts(session: requests.Session, cik: int, user_agent: str) -> dict:
    hit = _facts_cache.get(cik)
    if hit and time.time() - hit[0] < FACTS_TTL:
        return hit[1]
    r = session.get(FACTS_URL.format(cik=cik), headers={"User-Agent": user_agent}, timeout=60)
    r.raise_for_status()
    data = r.json() or {}
    _facts_cache[cik] = (time.time(), data)
    return data


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days + 1


def _currency(gaap: dict) -> str | None:
    """报表货币：收入或总资产等概念最常用的货币单位（中概股的 20-F 多为 CNY）。"""
    for concept in ("Assets", "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "NetIncomeLoss"):
        units = (gaap.get(concept) or {}).get("units") or {}
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


def statement_from_facts(facts: dict, kind: str, periods: int = 6, annual_only: bool = False) -> dict | None:
    """companyfacts → {"currency", "periods": [{"date", "label"}], "rows": [(科目, [各期金额])]}。
    列取第一个有数据的科目的期间：流量表每个截止日取最短的一段（10-Q 的利润表给 3 个月，现金流量表只有年初至今累计）；
    annual_only 时只取约 12 个月的期间（资产负债表取年报中出现的时点）。"""
    gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    cur = _currency(gaap)
    if not cur:
        return None
    instant = kind == "balance"
    rows: list[tuple[str, dict]] = []
    for label, concepts in US_ITEMS[kind]:
        unit = f"{cur}/shares" if "每股" in label else cur
        merged: dict[tuple, float] = {}
        for c in reversed(concepts):  # 公司换用过概念时逐期合并，靠前的概念优先
            merged.update({k: v for k, v in _concept_values(gaap, c, unit).items() if (k[0] is None) == instant})
        if merged:
            rows.append((label, merged))
    if not rows:
        return None
    anchor = rows[0][1]
    annual_ends = _annual_instants(gaap, cur) if instant and annual_only else None
    chosen: dict[str, tuple] = {}
    for start, end in anchor:
        if instant:
            if annual_ends is None or end in annual_ends:
                chosen[end] = (None, end)
            continue
        days = _days(start, end)
        if days < 80 or (annual_only and not 350 <= days <= 380):
            continue
        prev = chosen.get(end)
        if prev is None or days < _days(prev[0], end):
            chosen[end] = (start, end)
    keys = [chosen[e] for e in sorted(chosen, reverse=True)[:periods]]
    return {
        "currency": cur,
        "periods": [{"date": end, "label": end if start is None else
                     f"{end}（{'全年' if _days(start, end) >= 350 else f'{round(_days(start, end) / 30.44)}个月'}）"}
                    for start, end in keys],
        "rows": [(label, [vals.get(k) for k in keys]) for label, vals in rows if any(vals.get(k) is not None for k in keys)],
    }


def _annual_instants(gaap: dict, unit: str) -> set[str]:
    """年报（10-K / 20-F）中出现过的资产负债表日。"""
    return {f["end"] for f in ((gaap.get("Assets") or {}).get("units") or {}).get(unit) or [] if f.get("form") in ANNUAL_FORMS}
