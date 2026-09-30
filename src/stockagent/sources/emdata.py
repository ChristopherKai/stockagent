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
