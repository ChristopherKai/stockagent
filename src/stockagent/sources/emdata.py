"""东方财富数据中心：结构化财务指标与现金流量表/资产负债表（A 股 / 港股）、卖方研报与盈利预测（A 股）、A 股估值历史。

接口域名（datacenter / reportapi）与行情接口（push2）不同，行情被限流时这里通常仍可用。
"""

from __future__ import annotations

from datetime import date, timedelta

import requests

from ..config import Listing
from ..utils import to_float

DC = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
DC_WEB = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORTS = "https://reportapi.eastmoney.com/report/list"

# (字段, 中文名, 是否为金额)
A_FIELDS = [
    ("REPORT_DATE_NAME", "期间", False), ("NOTICE_DATE", "公告日", False),
    ("TOTALOPERATEREVE", "营业收入", True), ("TOTALOPERATEREVETZ", "收入同比%", False),
    ("MLR", "毛利", True), ("XSMLL", "毛利率%", False), ("PARENTNETPROFIT", "归母净利", True),
    ("PARENTNETPROFITTZ", "归母净利同比%", False), ("KCFJCXSYJLR", "扣非净利", True), ("XSJLL", "净利率%", False),
    ("ROEJQ", "ROE%", False), ("EPSJB", "EPS", False), ("BPS", "每股净资产", False), ("MGJYXJJE", "每股经营现金流", False),
    ("ZCFZL", "资产负债率%", False), ("YSZKZZTS", "应收账款周转天数", False), ("CHZZTS", "存货周转天数", False),
    ("CURRENCY", "币种", False),
]
HK_FIELDS = [
    ("REPORT_TYPE", "期间", False), ("OPERATE_INCOME", "营业额", True), ("OPERATE_INCOME_YOY", "营业额同比%", False),
    ("GROSS_PROFIT", "毛利", True), ("GROSS_PROFIT_RATIO", "毛利率%", False), ("HOLDER_PROFIT", "归母净利", True),
    ("HOLDER_PROFIT_YOY", "归母净利同比%", False), ("NET_PROFIT_RATIO", "净利率%", False), ("ROE_YEARLY", "ROE年化%", False),
    ("ROIC_YEARLY", "ROIC年化%", False), ("BASIC_EPS", "EPS", False), ("EPS_TTM", "EPS(TTM)", False), ("BPS", "每股净资产", False),
    ("NETCASH_OPERATE", "经营现金流", True), ("END_CASH", "期末现金", True), ("TOTAL_LIABILITIES", "总负债", True),
    ("DEBT_ASSET_RATIO", "资产负债率%", False), ("CURRENT_RATIO", "流动比率", False), ("ACCOUNTS_RECE_TDAYS", "应收周转率", False),
    ("CURRENCY", "币种", False),
]


def secucode(listing: Listing) -> str:
    return f"{listing.code}.HK" if listing.market == "HK" else f"{listing.code}.{listing.exchange}"


def _rows(session: requests.Session, url: str, **params) -> list[dict]:
    r = session.get(url, params=params, timeout=20)
    r.raise_for_status()
    result = (r.json() or {}).get("result") or {}
    return result.get("data") or []


def financials(session: requests.Session, listing: Listing, periods: int = 8) -> list[dict]:
    """最近 periods 期的主要财务指标（按报告期倒序），字段名为中文。"""
    if listing.market == "A":
        rows = _rows(session, DC, reportName="RPT_F10_FINANCE_MAINFINADATA", columns="ALL",
                     filter=f'(SECUCODE="{secucode(listing)}")', pageNumber=1, pageSize=periods,
                     sortTypes=-1, sortColumns="REPORT_DATE")
        fields = A_FIELDS
    elif listing.market == "HK":
        rows = _rows(session, DC, reportName="RPT_HKF10_FN_MAININDICATOR", columns="ALL",
                     filter=f'(SECUCODE="{secucode(listing)}")', pageNumber=1, pageSize=periods,
                     sortTypes=-1, sortColumns="STD_REPORT_DATE")
        fields = HK_FIELDS
    else:
        return []
    out = []
    for r in rows:
        item = {}
        for key, label, _ in fields:
            v = r.get(key)
            item[label] = v[:10] if isinstance(v, str) and key.endswith("DATE") else v
        item["_report_date"] = (r.get("REPORT_DATE") or r.get("STD_REPORT_DATE") or "")[:10]
        out.append(item)
    if listing.market == "HK":
        cur = infer_hk_currency(rows)
        for item in out:
            item["币种"] = cur
    return out


# 自由现金流与 EV 所需的报表科目。港股报表是「科目名 + 金额」的长表，A 股是宽表。
HK_OCF, HK_CAPEX = "经营业务现金净额", ("购建固定资产", "购建无形资产及其他资产")
HK_INV = "投资业务现金净额"  # 资本开支科目缺失或归类异常时的估算依据（见 fcf.metrics）
HK_CASH, HK_DEBT = ("现金及等价物", "短期存款", "中长期存款"), ("短期贷款", "长期贷款", "融资租赁负债(流动)", "融资租赁负债(非流动)")
HK_INVEST, HK_RESTRICTED = ("短期投资",), "受限制存款及现金"  # 受限制现金多为应付票据等的保证金，不计入 EV 的现金
A_CASH, A_INVEST = {"MONETARYFUNDS": "货币资金"}, {"TRADE_FINASSET": "交易性金融资产", "TRADE_FINASSET_NOTFVTPL": "交易性金融资产"}
A_DEBT = {"SHORT_LOAN": "短期借款", "LONG_LOAN": "长期借款", "BOND_PAYABLE": "应付债券", "SHORT_BOND_PAYABLE": "短期应付债券",
          "NONCURRENT_LIAB_1YEAR": "一年内到期的非流动负债", "LEASE_LIAB": "租赁负债"}


def fcf_data(session: requests.Session, listing: Listing) -> dict | None:
    """自由现金流与 EV 所需的报表数据（单位：元，报告币种）：
    {"flows": [{"date", "annual", "ocf", "capex"}]（报告期倒序；中报/季报为年初至今累计），
     "balance": {"date", "cash", "invest", "debt", "debt_items", "minority", "liabilities"}}"""
    code = f'(SECUCODE="{secucode(listing)}")'
    if listing.market == "A":
        cf = _rows(session, DC, reportName="RPT_F10_FINANCE_GCASHFLOW", columns="ALL", filter=code,
                   pageNumber=1, pageSize=20, sortTypes=-1, sortColumns="REPORT_DATE")
        bs = _rows(session, DC, reportName="RPT_F10_FINANCE_GBALANCE", columns="ALL", filter=code,
                   pageNumber=1, pageSize=1, sortTypes=-1, sortColumns="REPORT_DATE")
        return a_fcf_from_rows(cf, bs[0]) if cf and bs else None
    if listing.market != "HK":
        return None
    cf = _rows(session, DC, reportName="RPT_HKF10_FN_CASHFLOW_PC", columns="ALL", filter=code,
               pageNumber=1, pageSize=500, sortTypes=-1, sortColumns="REPORT_DATE")
    bs = _rows(session, DC, reportName="RPT_HKF10_FN_BALANCE_PC", columns="ALL", filter=code,
               pageNumber=1, pageSize=200, sortTypes=-1, sortColumns="REPORT_DATE")
    return hk_fcf_from_rows(cf, bs) if cf and bs else None


def a_fcf_from_rows(cf: list[dict], b: dict) -> dict:
    """A 股：现金流量表各期（宽表）+ 最新一期资产负债表 → fcf_data 形状。"""
    flows = [{"date": r["REPORT_DATE"][:10], "annual": r["REPORT_DATE"][5:10] == "12-31",
              "ocf": to_float(r.get("NETCASH_OPERATE")), "capex": abs(to_float(r.get("CONSTRUCT_LONG_ASSET")) or 0)}
             for r in cf]
    debt_items = {label: v for k, label in A_DEBT.items() if (v := to_float(b.get(k)))}
    cash_items = {label: v for k, label in A_CASH.items() if (v := to_float(b.get(k)))}
    balance = {"date": b["REPORT_DATE"][:10], "cash": sum(cash_items.values()), "cash_items": cash_items,
               "invest": sum(to_float(b.get(k)) or 0 for k in A_INVEST), "debt": sum(debt_items.values()),
               "debt_items": debt_items, "minority": to_float(b.get("MINORITY_EQUITY")) or 0,
               "liabilities": to_float(b.get("TOTAL_LIABILITIES")), "restricted": 0.0}
    return {"flows": sorted(flows, key=lambda x: x["date"], reverse=True), "balance": balance}


def hk_fcf_from_rows(cf: list[dict], bs: list[dict]) -> dict | None:
    """港股：现金流量表与资产负债表的「科目名 + 金额」长表 → fcf_data 形状（资产负债表取最新一期）。"""
    if not cf or not bs:
        return None
    periods: dict[str, dict] = {}
    for r in cf:
        d = periods.setdefault(r["REPORT_DATE"][:10], {"date": r["REPORT_DATE"][:10], "annual": r.get("DATE_TYPE_CODE") == "001",
                                                       "ocf": None, "capex": 0.0, "inv": None})
        amount = to_float(r.get("AMOUNT"))
        if r.get("STD_ITEM_NAME") == HK_OCF:
            d["ocf"] = amount
        elif r.get("STD_ITEM_NAME") == HK_INV:
            d["inv"] = amount
        elif r.get("STD_ITEM_NAME") in HK_CAPEX and amount:
            d["capex"] += abs(amount)
    latest = max(r["REPORT_DATE"][:10] for r in bs)
    items = {r["STD_ITEM_NAME"]: to_float(r.get("AMOUNT")) for r in bs if r["REPORT_DATE"][:10] == latest}
    debt_items = {k: v for k, v in items.items() if v and (k in HK_DEBT or ("债券" in k and "衍生" not in k))}
    cash_items = {k: v for k in HK_CASH if (v := items.get(k))}
    balance = {"date": latest, "cash": sum(cash_items.values()), "cash_items": cash_items,
               "invest": sum(items.get(k) or 0 for k in HK_INVEST), "debt": sum(debt_items.values()),
               "debt_items": debt_items, "minority": items.get("少数股东权益") or 0, "liabilities": items.get("总负债"),
               "restricted": items.get(HK_RESTRICTED) or 0.0}
    return {"flows": sorted(periods.values(), key=lambda x: x["date"], reverse=True), "balance": balance}


def ttm_flow(flows: list[dict]) -> dict | None:
    """最近 12 个月的经营现金流与资本开支：最新一期若是中报/季报（年初至今累计），
    TTM = 本期累计 + 上一年度 − 上年同期累计。"""
    if not flows or flows[0]["ocf"] is None:
        return None
    last = flows[0]
    if last["annual"]:
        return {**last, "label": f"{last['date'][:4]} 年报"}
    year, tail = int(last["date"][:4]), last["date"][4:]
    fy = next((f for f in flows if f["annual"] and f["date"][:4] == str(year - 1)), None)
    same = next((f for f in flows if f["date"] == f"{year - 1}{tail}"), None)
    if not fy or not same or None in (fy["ocf"], same["ocf"]):
        return None
    return {"date": last["date"], "label": f"TTM（截至 {last['date']}）",
            "ocf": last["ocf"] + fy["ocf"] - same["ocf"], "capex": last["capex"] + fy["capex"] - same["capex"]}


def infer_hk_currency(rows: list[dict]) -> str:
    """港股数据的「CURRENCY」字段一律是 HKD，但金额实际是公司的报告币种（内地公司多为人民币）。
    用同一行的 总市值 ÷ 股本 ÷ PE(TTM) 反推港元口径 EPS，与报表 EPS(TTM) 相比：约 1 为港元，约 1.05–1.35 为人民币，约 6–9 为美元。"""
    for r in rows:
        mcap, shares, pe, eps = (to_float(r.get(k)) for k in ("TOTAL_MARKET_CAP", "ISSUED_COMMON_SHARES", "PE_TTM", "EPS_TTM"))
        if not (mcap and shares and pe and eps) or abs(eps) < 1e-9:
            continue
        ratio = (mcap / shares / pe) / eps
        if 0.95 <= ratio <= 1.05:
            return "HKD"
        if 1.05 < ratio <= 1.35:
            return "CNY"
        if 6 <= ratio <= 9:
            return "USD"
    return "CNY"  # 无法判断时按内地公司最常见的人民币处理


def money_fields(market: str) -> set[str]:
    return {label for _, label, is_money in (A_FIELDS if market == "A" else HK_FIELDS) if is_money}


def research_reports(session: requests.Session, a_code: str, months: int = 6, size: int = 40) -> list[dict]:
    """A 股个股研报（卖方评级与盈利预测）。港股不在该库中。"""
    end = date.today()
    begin = end - timedelta(days=int(months * 30.5))
    r = session.get(REPORTS, params={
        "industryCode": "*", "pageSize": size, "industry": "*", "rating": "*", "ratingChange": "*",
        "beginTime": begin.isoformat(), "endTime": end.isoformat(), "pageNo": 1, "fields": "", "qType": 0,
        "orgCode": "", "code": a_code, "rcode": "",
    }, timeout=20)
    r.raise_for_status()
    out = []
    for x in (r.json() or {}).get("data") or []:
        out.append({
            "date": (x.get("publishDate") or "")[:10], "org": x.get("orgSName") or x.get("orgName"),
            "title": x.get("title"), "rating": x.get("emRatingName") or x.get("sRatingName") or "",
            "rating_change": x.get("ratingChange"),
            "eps": [to_float(x.get(k)) for k in ("predictThisYearEps", "predictNextYearEps", "predictNextTwoYearEps")],
            "pe": [to_float(x.get(k)) for k in ("predictThisYearPe", "predictNextYearPe", "predictNextTwoYearPe")],
            "target": to_float(x.get("indvAimPriceT")),
        })
    return out


def valuation_history_a(session: requests.Session, a_code: str, days: int = 1250) -> list[tuple[str, float | None, float | None]]:
    """A 股每日 PE(TTM)、PB(MRQ) 历史，按日期正序：[(日期, PE, PB)]。"""
    out: list[tuple[str, float | None, float | None]] = []
    page, size = 1, 500
    while len(out) < days:
        rows = _rows(session, DC_WEB, reportName="RPT_VALUEANALYSIS_DET", columns="TRADE_DATE,PE_TTM,PB_MRQ",
                     filter=f'(SECURITY_CODE="{a_code}")', pageNumber=page, pageSize=size,
                     sortTypes=-1, sortColumns="TRADE_DATE")
        if not rows:
            break
        out += [(r["TRADE_DATE"][:10], to_float(r.get("PE_TTM")), to_float(r.get("PB_MRQ"))) for r in rows]
        if len(rows) < size:
            break
        page += 1
    return sorted(out[:days])


def southbound_holdings(session: requests.Session, hk_code: str, rows: int = 160) -> list[dict]:
    """港股通（南向资金）持股：中国结算作为名义持有人的持股数与占比，按日期正序。"""
    data = _rows(session, DC_WEB, reportName="RPT_MUTUAL_HOLD_DET", columns="HOLD_DATE,HOLD_NUM,HOLD_SHARES_RATIO,HOLD_MARKET_CAP,ORG_NAME",
                 filter=f'(SECURITY_CODE="{hk_code}")', pageNumber=1, pageSize=rows, sortTypes=-1, sortColumns="HOLD_DATE")
    # 同一天有两行：沪港通与深港通各自的中国结算名义持有账户（中文名/英文名），需要相加
    by_date: dict[str, dict] = {}
    for r in data:
        if not r.get("HOLD_DATE"):
            continue
        d = by_date.setdefault(r["HOLD_DATE"][:10], {"date": r["HOLD_DATE"][:10], "shares": 0.0, "ratio": 0.0, "mcap": 0.0})
        for key, field in (("shares", "HOLD_NUM"), ("ratio", "HOLD_SHARES_RATIO"), ("mcap", "HOLD_MARKET_CAP")):
            d[key] += to_float(r.get(field)) or 0.0
    return sorted(by_date.values(), key=lambda x: x["date"])


def southbound_summary(series: list[dict]) -> dict | None:
    """最新持股占比，以及 5/20/60 个交易日前以来的持股变化（股数与占比百分点）。"""
    if not series:
        return None
    last = series[-1]
    out = {"date": last["date"], "ratio": last["ratio"], "shares": last["shares"]}
    for n in (5, 20, 60):
        if len(series) > n and series[-1 - n]["shares"]:
            prev = series[-1 - n]
            out[f"chg_shares_{n}"] = last["shares"] - prev["shares"]
            out[f"chg_ratio_{n}"] = (last["ratio"] or 0) - (prev["ratio"] or 0)
    return out


# ---------------------------------------------------------------- 公司概况（新增公司时自动填写）

PROFILE_REPORTS = {"A": "RPT_F10_BASIC_ORGINFO", "HK": "RPT_HKF10_INFO_ORGPROFILE", "US": "RPT_USF10_INFO_ORGPROFILE"}
US_EXCHANGES = {"O": "NASDAQ", "N": "NYSE", "A": "AMEX"}  # 东方财富美股 SECUCODE 后缀；其他后缀（如 .F 场外）不算上市地


def _profile_listing(market: str, secucode_: str) -> tuple[str, str | None] | None:
    """SECUCODE → (代码, 交易所)；美股场外等不支持的返回 None。"""
    code, _, suffix = secucode_.rpartition(".")
    if market == "HK":
        return code, None
    if market == "A":  # 只认 6 位数字：未上市的 CDR 等在这里是占位代码（如小米的 A17348）
        return (code, suffix) if suffix in ("SH", "SZ", "BJ") and len(code) == 6 and code.isdigit() else None
    return (code, US_EXCHANGES[suffix]) if suffix in US_EXCHANGES else None


def company_profile(session: requests.Session, market: str, code: str) -> dict | None:
    """按市场与代码查公司概况：{name, name_en, industry, business, listings: [(市场, 代码, 交易所)]}。
    同一家公司在 A 股、港股、美股的 ORG_CODE 相同，据此找出其他上市地（A+H、港股+美股）。"""
    if market == "US":
        flt = f'(SECURITY_CODE="{code}")'
    else:
        l = Listing(market=market, code=code)
        flt = f'(SECURITY_CODE="{l.code}")' if market == "A" else f'(SECUCODE="{l.code}.HK")'
    rows = [r for r in _rows(session, DC, reportName=PROFILE_REPORTS[market], columns="ALL", filter=flt, pageSize=10)
            if _profile_listing(market, r.get("SECUCODE") or "")]
    if not rows:
        return None
    row = rows[0]
    profiles = {market: row}
    listings = [(market, *_profile_listing(market, row["SECUCODE"]))]
    for other in ("A", "HK", "US"):
        if other == market or not row.get("ORG_CODE"):
            continue
        found = [r for r in _rows(session, DC, reportName=PROFILE_REPORTS[other], columns="ALL",
                                  filter=f'(ORG_CODE="{row["ORG_CODE"]}")', pageSize=10)
                 if _profile_listing(other, r.get("SECUCODE") or "")]
        if found:
            profiles[other] = found[0]
            listings.append((other, *_profile_listing(other, found[0]["SECUCODE"])))
    order = [m for m in ("A", "HK", "US") if m in profiles]  # 名称优先取 A 股简称
    first = lambda *keys: next((str(profiles[m][k]).strip() for m in order for k in keys if profiles[m].get(k)), None)
    return {
        "name": first("SECURITY_NAME_ABBR"),
        "names": [str(profiles[m]["SECURITY_NAME_ABBR"]).strip() for m in order if profiles[m].get("SECURITY_NAME_ABBR")],
        "name_en": first("ORG_NAME_EN", "ORG_EN_ABBR"),
        "industry": first("BOARD_NAME_LEVEL", "EM2016", "INDUSTRY_TYPE", "BELONG_INDUSTRY"),
        "business": first("MAIN_BUSINESS", "ORG_PROFILE"),
        "listings": sorted(listings, key=lambda x: ("A", "HK", "US").index(x[0])),
    }


# ---------------------------------------------------------------- 三张报表（完整科目）

STATEMENT_KINDS = {"income": "利润表", "balance": "资产负债表", "cashflow": "现金流量表"}
A_STATEMENT_REPORTS = {"income": "RPT_F10_FINANCE_GINCOME", "balance": "RPT_F10_FINANCE_GBALANCE", "cashflow": "RPT_F10_FINANCE_GCASHFLOW"}
HK_STATEMENT_REPORTS = {"income": "RPT_HKF10_FN_INCOME_PC", "balance": "RPT_HKF10_FN_BALANCE_PC", "cashflow": "RPT_HKF10_FN_CASHFLOW_PC"}
# A 股宽表（一般工商业格式）中要展示的科目，按报表顺序；银行、保险、券商的科目不同，这里取不到时如实返回
A_STATEMENT_ITEMS = {
    "income": [
        ("TOTAL_OPERATE_INCOME", "营业总收入"), ("OPERATE_INCOME", "营业收入"), ("TOTAL_OPERATE_COST", "营业总成本"),
        ("OPERATE_COST", "营业成本"), ("OPERATE_TAX_ADD", "税金及附加"), ("SALE_EXPENSE", "销售费用"),
        ("MANAGE_EXPENSE", "管理费用"), ("RESEARCH_EXPENSE", "研发费用"), ("FINANCE_EXPENSE", "财务费用"),
        ("FE_INTEREST_EXPENSE", "其中：利息费用"), ("FE_INTEREST_INCOME", "其中：利息收入"), ("OTHER_INCOME", "其他收益"),
        ("INVEST_INCOME", "投资收益"), ("INVEST_JOINT_INCOME", "其中：对联营合营企业的投资收益"),
        ("FAIRVALUE_CHANGE_INCOME", "公允价值变动收益"), ("CREDIT_IMPAIRMENT_INCOME", "信用减值损失"),
        ("ASSET_IMPAIRMENT_INCOME", "资产减值损失"), ("ASSET_DISPOSAL_INCOME", "资产处置收益"), ("OPERATE_PROFIT", "营业利润"),
        ("NONBUSINESS_INCOME", "营业外收入"), ("NONBUSINESS_EXPENSE", "营业外支出"), ("TOTAL_PROFIT", "利润总额"),
        ("INCOME_TAX", "所得税费用"), ("NETPROFIT", "净利润"), ("PARENT_NETPROFIT", "归属于母公司股东的净利润"),
        ("MINORITY_INTEREST", "少数股东损益"), ("DEDUCT_PARENT_NETPROFIT", "扣除非经常性损益后的归母净利润"),
        ("BASIC_EPS", "基本每股收益"), ("DILUTED_EPS", "稀释每股收益"),
    ],
    "balance": [
        ("MONETARYFUNDS", "货币资金"), ("TRADE_FINASSET", "交易性金融资产"), ("TRADE_FINASSET_NOTFVTPL", "交易性金融资产"),
        ("NOTE_RECE", "应收票据"), ("ACCOUNTS_RECE", "应收账款"), ("FINANCE_RECE", "应收款项融资"), ("PREPAYMENT", "预付款项"),
        ("TOTAL_OTHER_RECE", "其他应收款合计"), ("INVENTORY", "存货"), ("CONTRACT_ASSET", "合同资产"),
        ("OTHER_CURRENT_ASSET", "其他流动资产"), ("TOTAL_CURRENT_ASSETS", "流动资产合计"), ("LONG_EQUITY_INVEST", "长期股权投资"),
        ("OTHER_NONCURRENT_FINASSET", "其他非流动金融资产"), ("FIXED_ASSET", "固定资产"), ("CIP", "在建工程"),
        ("USERIGHT_ASSET", "使用权资产"), ("INTANGIBLE_ASSET", "无形资产"), ("DEVELOP_EXPENSE", "开发支出"), ("GOODWILL", "商誉"),
        ("DEFER_TAX_ASSET", "递延所得税资产"), ("OTHER_NONCURRENT_ASSET", "其他非流动资产"),
        ("TOTAL_NONCURRENT_ASSETS", "非流动资产合计"), ("TOTAL_ASSETS", "资产总计"), ("SHORT_LOAN", "短期借款"),
        ("NOTE_PAYABLE", "应付票据"), ("ACCOUNTS_PAYABLE", "应付账款"), ("ADVANCE_RECEIVABLES", "预收款项"),
        ("CONTRACT_LIAB", "合同负债"), ("STAFF_SALARY_PAYABLE", "应付职工薪酬"), ("TAX_PAYABLE", "应交税费"),
        ("TOTAL_OTHER_PAYABLE", "其他应付款合计"), ("NONCURRENT_LIAB_1YEAR", "一年内到期的非流动负债"),
        ("TOTAL_CURRENT_LIAB", "流动负债合计"), ("LONG_LOAN", "长期借款"), ("BOND_PAYABLE", "应付债券"), ("LEASE_LIAB", "租赁负债"),
        ("LONG_PAYABLE", "长期应付款"), ("TOTAL_NONCURRENT_LIAB", "非流动负债合计"), ("TOTAL_LIABILITIES", "负债合计"),
        ("SHARE_CAPITAL", "股本"), ("CAPITAL_RESERVE", "资本公积"), ("TREASURY_SHARES", "库存股"),
        ("UNASSIGN_RPOFIT", "未分配利润"), ("TOTAL_PARENT_EQUITY", "归属于母公司股东权益合计"), ("MINORITY_EQUITY", "少数股东权益"),
        ("TOTAL_EQUITY", "股东权益合计"),
    ],
    "cashflow": [
        ("SALES_SERVICES", "销售商品、提供劳务收到的现金"), ("RECEIVE_TAX_REFUND", "收到的税费返还"),
        ("BUY_SERVICES", "购买商品、接受劳务支付的现金"), ("PAY_STAFF_CASH", "支付给职工的现金"), ("PAY_ALL_TAX", "支付的各项税费"),
        ("NETCASH_OPERATE", "经营活动产生的现金流量净额"), ("RECEIVE_INVEST_INCOME", "取得投资收益收到的现金"),
        ("DISPOSAL_LONG_ASSET", "处置长期资产收回的现金"), ("CONSTRUCT_LONG_ASSET", "购建长期资产支付的现金"),
        ("INVEST_PAY_CASH", "投资支付的现金"), ("NETCASH_INVEST", "投资活动产生的现金流量净额"),
        ("ACCEPT_INVEST_CASH", "吸收投资收到的现金"), ("RECEIVE_LOAN_CASH", "取得借款收到的现金"),
        ("PAY_DEBT_CASH", "偿还债务支付的现金"), ("ASSIGN_DIVIDEND_PORFIT", "分配股利、利润或偿付利息支付的现金"),
        ("NETCASH_FINANCE", "筹资活动产生的现金流量净额"), ("CCE_ADD", "现金及现金等价物净增加额"),
        ("END_CCE", "期末现金及现金等价物余额"), ("FA_IR_DEPR", "固定资产折旧"), ("IA_AMORTIZE", "无形资产摊销"),
        ("USERIGHT_ASSET_AMORTIZE", "使用权资产折旧"), ("INVENTORY_REDUCE", "存货的减少"),
        ("OPERATE_RECE_REDUCE", "经营性应收项目的减少"), ("OPERATE_PAYABLE_ADD", "经营性应付项目的增加"),
    ],
}
HK_SKIP_ITEMS = {"非运算项目"}  # 数据商的轧差项，不是报表科目


def _months(start: str | None, end: str) -> int | None:
    if not start:
        return None
    s, e = date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
    return max(1, round(((e - s).days + 1) / 30.44))


def _period_label(end: str, months: int | None, instant: bool) -> str:
    if instant or not months:
        return end
    return f"{end}（{'全年' if months >= 12 else f'{months}个月'}）"


def a_statement_from_rows(kind: str, rows: list[dict]) -> dict:
    """A 股宽表 → {"periods": [{"date", "label"}], "rows": [(科目, [各期金额])]}。利润表与现金流量表为年初至今累计。"""
    rows = sorted(rows, key=lambda r: r["REPORT_DATE"], reverse=True)
    periods = [{"date": r["REPORT_DATE"][:10],
                "label": _period_label(r["REPORT_DATE"][:10], None if kind == "balance" else int(r["REPORT_DATE"][5:7]), kind == "balance")}
               for r in rows]
    out, seen = [], set()
    for key, label in A_STATEMENT_ITEMS[kind]:
        values = [to_float(r.get(key)) for r in rows]
        if label in seen or not any(values):
            continue
        seen.add(label)
        out.append((label, values))
    return {"periods": periods, "rows": out}


def hk_statement_from_rows(kind: str, rows: list[dict], periods: int) -> dict:
    """港股「科目名 + 金额」长表 → 同上形状，科目按数据商的标准科目编号排序。"""
    by_date: dict[str, dict] = {}
    for r in rows:
        d = by_date.setdefault(r["REPORT_DATE"][:10], {"start": r.get("START_DATE"), "items": {}})
        if r.get("STD_ITEM_NAME") and r["STD_ITEM_NAME"] not in HK_SKIP_ITEMS:
            d["items"][r["STD_ITEM_NAME"]] = (r.get("STD_ITEM_CODE") or "", to_float(r.get("AMOUNT")))
    dates = sorted(by_date, reverse=True)[:periods]
    names: dict[str, str] = {}
    for d in dates:
        for name, (code, _) in by_date[d]["items"].items():
            names.setdefault(name, code)
    out = []
    for name in sorted(names, key=lambda n: names[n]):
        values = [by_date[d]["items"].get(name, ("", None))[1] for d in dates]
        if any(values):
            out.append((name, values))
    return {"periods": [{"date": d, "label": _period_label(d, _months(by_date[d]["start"], d), kind == "balance")} for d in dates],
            "rows": out}


def statement(session: requests.Session, listing: Listing, kind: str, periods: int = 6, annual_only: bool = False) -> dict | None:
    """完整报表（金额单位：元，报告币种）。annual_only 只取年报。港股的币种由调用方用 financials 推断。"""
    code = f'(SECUCODE="{secucode(listing)}")'
    if listing.market == "A":
        flt = code + ('(REPORT_TYPE="年报")' if annual_only else "")
        rows = _rows(session, DC, reportName=A_STATEMENT_REPORTS[kind], columns="ALL", filter=flt,
                     pageNumber=1, pageSize=periods, sortTypes=-1, sortColumns="REPORT_DATE")
        return a_statement_from_rows(kind, rows) if rows else None
    if listing.market != "HK":
        return None
    flt = code + ('(DATE_TYPE_CODE="001")' if annual_only else "")
    rows = _rows(session, DC, reportName=HK_STATEMENT_REPORTS[kind], columns="ALL", filter=flt,
                 pageNumber=1, pageSize=500, sortTypes=-1, sortColumns="REPORT_DATE")
    return hk_statement_from_rows(kind, rows, periods) if rows else None


# ---------------------------------------------------------------- 股东

def a_holders(session: requests.Session, a_code: str, free: bool = False, periods: int = 2) -> list[dict]:
    """A 股十大股东（free=True 为十大流通股东），最近 periods 个报告期，按期倒序、名次正序。"""
    report = "RPT_F10_EH_FREEHOLDERS" if free else "RPT_F10_EH_HOLDERS"
    rows = _rows(session, DC, reportName=report, columns="ALL", filter=f'(SECURITY_CODE="{a_code}")',
                 pageNumber=1, pageSize=10 * periods, sortTypes="-1,1", sortColumns="END_DATE,HOLDER_RANK")
    return [{"date": (r.get("END_DATE") or "")[:10], "rank": r.get("HOLDER_RANK"), "name": r.get("HOLDER_NAME"),
             "shares": to_float(r.get("HOLD_NUM")),
             "ratio": to_float(r.get("FREE_HOLDNUM_RATIO") if free else r.get("HOLD_NUM_RATIO")),
             "change": r.get("HOLD_NUM_CHANGE") or "", "type": r.get("HOLDER_NEWTYPE") or r.get("HOLDER_TYPE") or "",
             "share_type": r.get("SHARES_TYPE") or ""} for r in rows]


def a_holder_counts(session: requests.Session, a_code: str, periods: int = 8) -> list[dict]:
    """A 股股东户数与户均持股，按期倒序。"""
    rows = _rows(session, DC, reportName="RPT_F10_EH_HOLDERNUM", columns="ALL", filter=f'(SECURITY_CODE="{a_code}")',
                 pageNumber=1, pageSize=periods, sortTypes=-1, sortColumns="END_DATE")
    return [{"date": (r.get("END_DATE") or "")[:10], "holders": to_float(r.get("HOLDER_TOTAL_NUM")),
             "change_pct": to_float(r.get("TOTAL_NUM_RATIO")), "avg_shares": to_float(r.get("AVG_FREE_SHARES")),
             "focus": r.get("HOLD_FOCUS") or "", "top10_ratio": to_float(r.get("HOLD_RATIO_TOTAL"))} for r in rows]


def hk_holders(session: requests.Session, hk_code: str) -> list[dict]:
    """港股主要股东与董事的权益披露（数据商按公司最新的定期报告整理），取最新报告期，按持股比例倒序。"""
    rows = _rows(session, DC, reportName="RPT_HKF10_EQUITYCHG_HOLDER", columns="ALL",
                 filter=f'(SECUCODE="{Listing(market="HK", code=hk_code).code}.HK")',
                 pageNumber=1, pageSize=200, sortTypes=-1, sortColumns="REPORT_DATE")
    if not rows:
        return []
    latest = max((r.get("REPORT_DATE") or "") for r in rows)
    out = [{"date": latest[:10], "notice": (r.get("NOTICE_DATE") or "")[:10], "name": r.get("HOLDER_NAME"),
            "shares": to_float(r.get("TOTAL_SHARES")), "ratio": to_float(r.get("TOTAL_SHARES_RATIO")),
            "role": r.get("EQUITY_TYPE") or "", "identity": r.get("HOLD_IDENTITY") or "", "share_type": r.get("SHARES_TYPE") or ""}
           for r in rows if r.get("REPORT_DATE") == latest]
    return sorted(out, key=lambda x: x["ratio"] or 0, reverse=True)
