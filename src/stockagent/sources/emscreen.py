"""东方财富数据中心：全市场批量数据（选股器用）。

与 emdata 的单公司查询共用报表科目口径（emdata.hk_fcf_from_rows / a_fcf_from_rows）。
只用数据中心域名（datacenter / datacenter-web），不用行情列表接口（push2 clist）——后者对批量翻页限流很严，
被限流时连单只行情快照也会一起失败，影响日常流程。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

import requests

from .emdata import (
    A_CASH,
    A_DEBT,
    A_INVEST,
    DC,
    DC_WEB,
    HK_CAPEX,
    HK_CASH,
    HK_DEBT,
    HK_INV,
    HK_INVEST,
    HK_OCF,
    HK_RESTRICTED,
    hk_fcf_from_rows,
)

PAGE = 500
CHUNK = 100  # 「SECUCODE in (...)」每批代码数

# 港股资产负债表中名称含「债券」的有息负债（批量查询不支持 like，只能列出科目名）
HK_BONDS = ("已发行债券", "可转换票据及债券", "可转换债券", "应付债券", "应付债券(流动)", "应付债券(非流动)", "债券")
HK_BS_ITEMS = (*HK_CASH, *HK_DEBT, *HK_INVEST, HK_RESTRICTED, *HK_BONDS, "少数股东权益", "总负债")
HK_CF_ITEMS = (HK_OCF, *HK_CAPEX, HK_INV)
A_TYPE = '(SECURITY_TYPE_CODE="058001001")'  # A 股（排除新三板、B 股等）

A_BS_COLS = ["SECUCODE", "REPORT_DATE", "NOTICE_DATE", *A_CASH, *A_INVEST, *A_DEBT, "MINORITY_EQUITY", "TOTAL_LIABILITIES"]
A_CF_COLS = ["SECUCODE", "REPORT_DATE", "NOTICE_DATE", "NETCASH_OPERATE", "CONSTRUCT_LONG_ASSET", "ASSIGN_DIVIDEND_PORFIT"]
A_IND_COLS = ["SECUCODE", "REPORT_DATE", "NOTICE_DATE", "ORG_TYPE", "TOTALOPERATEREVE", "TOTALOPERATEREVETZ", "PARENTNETPROFIT",
              "PARENTNETPROFITTZ", "KCFJCXSYJLRTZ", "XSMLL", "ROEJQ", "ZCFZL"]
HK_IND_COLS = ["SECUCODE", "STD_REPORT_DATE", "REPORT_DATE", "DATE_TYPE_CODE", "ORG_TYPE", "TOTAL_MARKET_CAP", "HKSK_MARKET_CAP",
               "ISSUED_COMMON_SHARES", "HK_COMMON_SHARES", "PE_TTM", "PB_TTM", "EPS_TTM", "DIVIDEND_RATE", "DIVI_RATIO",
               "OPERATE_INCOME", "OPERATE_INCOME_YOY", "HOLDER_PROFIT", "HOLDER_PROFIT_YOY", "GROSS_PROFIT_RATIO", "ROE_YEARLY",
               "DEBT_ASSET_RATIO", "FISCAL_YEAR"]


def _page(session: requests.Session, url: str, **params) -> tuple[list[dict], int]:
    r = session.get(url, params=params, timeout=40)
    r.raise_for_status()
    body = r.json() or {}
    result = body.get("result") or {}
    if not result and body.get("message") not in (None, "ok"):
        raise ValueError(f"{params.get('reportName')}：{body.get('message')}")
    return result.get("data") or [], int(result.get("pages") or 0)


def rows_all(session: requests.Session, url: str, sort: str, max_pages: int = 100, **params) -> list[dict]:
    """翻页取完一个查询的全部行。sort 必须能唯一确定行的顺序（如 "SECUCODE,REPORT_DATE,STD_ITEM_NAME"，列名前加 - 表示倒序）：
    数据中心不排序时各页的顺序不稳定，翻页会漏行、重行（实测同一科目被算两次）。"""
    cols = [c.strip() for c in sort.split(",")]
    params.update(sortColumns=",".join(c.lstrip("-") for c in cols), sortTypes=",".join("-1" if c.startswith("-") else "1" for c in cols))
    out: list[dict] = []
    page, pages = 1, 1
    while page <= min(pages, max_pages):
        rows, pages = _page(session, url, pageNumber=page, pageSize=PAGE, **params)
        out += rows
        if not rows:
            break
        page += 1
    return out


def count(session: requests.Session, url: str, **params) -> int:
    r = session.get(url, params={**params, "pageNumber": 1, "pageSize": 1}, timeout=30)
    r.raise_for_status()
    return int(((r.json() or {}).get("result") or {}).get("count") or 0)


def _in(field: str, values) -> str:
    return f'({field} in ({",".join(chr(34) + v + chr(34) for v in values)}))'


def _chunks(codes: list[str]):
    for i in range(0, len(codes), CHUNK):
        yield codes[i:i + CHUNK]


def hk_code(secucode: str) -> str:
    return secucode.split(".")[0]


# ---------------------------------------------------------------- 港股

def hk_connect(session: requests.Session) -> dict[str, dict]:
    """港股通标的（沪、深港股通任一）：{代码: {name, org_code, sh, sz, security_type, board, listing_date}}。"""
    out: dict[str, dict] = {}
    for field, key in (("GANGGUTONGBIAODIHU", "sh"), ("GANGGUTONGBIAODISHEN", "sz")):
        for r in rows_all(session, DC, "SECUCODE", reportName="RPT_HKF10_INFO_SECURITYINFO",
                          columns="SECUCODE,SECURITY_NAME_ABBR,ORG_CODE,SECURITY_TYPE,BOARD,LISTING_DATE", filter=f'({field}="是")'):
            d = out.setdefault(hk_code(r["SECUCODE"]), {"name": r.get("SECURITY_NAME_ABBR"), "org_code": r.get("ORG_CODE"),
                                                        "sh": False, "sz": False,
                                                        "security_type": r.get("SECURITY_TYPE"), "board": r.get("BOARD"),
                                                        "listing_date": (r.get("LISTING_DATE") or "")[:10] or None})
            d[key] = True
    return out


def hk_profiles(session: requests.Session, codes: list[str]) -> dict[str, dict]:
    """行业、机构类型（一般企业 / 银行 / 保险…）、核数师。"""
    out = {}
    for chunk in _chunks(codes):
        for r in rows_all(session, DC, "SECUCODE", reportName="RPT_HKF10_INFO_ORGPROFILE",
                          columns="SECUCODE,BELONG_INDUSTRY,INDUSTRY_TYPE,ORG_TYPE,ACCOUNT_FIRM,LISTING_DATE",
                          filter=_in("SECUCODE", [f"{c}.HK" for c in chunk])):
            out[hk_code(r["SECUCODE"])] = {"industry": r.get("BELONG_INDUSTRY"), "industry_type": r.get("INDUSTRY_TYPE"),
                                           "org_type": r.get("ORG_TYPE"), "auditor": r.get("ACCOUNT_FIRM"),
                                           "listing_date": (r.get("LISTING_DATE") or "")[:10] or None}
    return out


def hk_indicators(session: requests.Session, codes: list[str], since: str) -> dict[str, list[dict]]:
    """主要指标（含当前总市值、股息率、派息比率、利润增速、毛利率），每家按报告期倒序。
    TOTAL_MARKET_CAP 为全部股本（含 A 股）按港股现价计的港元市值，每日更新。"""
    out: dict[str, list[dict]] = defaultdict(list)
    for chunk in _chunks(codes):
        for r in rows_all(session, DC, "SECUCODE,STD_REPORT_DATE,REPORT_DATE", reportName="RPT_HKF10_FN_MAININDICATOR", columns=",".join(HK_IND_COLS),
                          filter=_in("SECUCODE", [f"{c}.HK" for c in chunk]) + f"(STD_REPORT_DATE>='{since}')"):
            out[hk_code(r["SECUCODE"])].append(r)
    for rows in out.values():
        rows.sort(key=lambda r: r["STD_REPORT_DATE"], reverse=True)
    return dict(out)


def hk_statements(session: requests.Session, codes: list[str], cf_since: str, bs_since: str) -> dict[str, dict]:
    """批量取现金流量表与资产负债表所需科目，按公司组装成 fcf_data 形状。"""
    cf: dict[str, list[dict]] = defaultdict(list)
    bs: dict[str, list[dict]] = defaultdict(list)
    cols = "SECUCODE,REPORT_DATE,STD_ITEM_NAME,AMOUNT,DATE_TYPE_CODE"
    for chunk in _chunks(codes):
        secu = _in("SECUCODE", [f"{c}.HK" for c in chunk])
        for r in rows_all(session, DC, "SECUCODE,REPORT_DATE,STD_ITEM_NAME", reportName="RPT_HKF10_FN_CASHFLOW_PC", columns=cols,
                          filter=secu + _in("STD_ITEM_NAME", HK_CF_ITEMS) + f"(REPORT_DATE>='{cf_since}')"):
            cf[hk_code(r["SECUCODE"])].append(r)
        for r in rows_all(session, DC, "SECUCODE,REPORT_DATE,STD_ITEM_NAME", reportName="RPT_HKF10_FN_BALANCE_PC", columns=cols,
                          filter=secu + _in("STD_ITEM_NAME", HK_BS_ITEMS) + f"(REPORT_DATE>='{bs_since}')"):
            bs[hk_code(r["SECUCODE"])].append(r)
    return {c: d for c in codes if (d := hk_fcf_from_rows(cf.get(c, []), bs.get(c, [])))}


# ---------------------------------------------------------------- A 股

def a_valuation(session: requests.Session) -> tuple[str | None, list[dict]]:
    """最新交易日的全市场估值快照：代码、名称、行业、总市值、收盘价、PE(TTM)、PB、PCF。"""
    head, _ = _page(session, DC_WEB, reportName="RPT_VALUEANALYSIS_DET", columns="TRADE_DATE", pageNumber=1, pageSize=1,
                    sortColumns="TRADE_DATE", sortTypes=-1)
    if not head:
        return None, []
    day = head[0]["TRADE_DATE"][:10]
    rows = rows_all(session, DC_WEB, "SECUCODE", reportName="RPT_VALUEANALYSIS_DET",
                    columns="SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,ORG_CODE,BOARD_NAME,TOTAL_MARKET_CAP,NOTLIMITED_MARKETCAP_A,"
                            "CLOSE_PRICE,TOTAL_SHARES,PE_TTM,PB_MRQ,PCF_OCF_TTM,TRADE_DATE",
                    filter=f"(TRADE_DATE='{day}')")
    return day, rows


def a_statements(session: requests.Session, kind: str, period: str) -> list[dict]:
    """某报告期全市场的资产负债表（bs）/ 现金流量表（cf）/ 主要指标（ind），只取选股需要的列。"""
    report, cols = {"bs": ("RPT_F10_FINANCE_GBALANCE", A_BS_COLS), "cf": ("RPT_F10_FINANCE_GCASHFLOW", A_CF_COLS),
                    "ind": ("RPT_F10_FINANCE_MAINFINADATA", A_IND_COLS)}[kind]
    return rows_all(session, DC, "SECUCODE", reportName=report, columns=",".join(cols), filter=f"(REPORT_DATE='{period}')" + A_TYPE)


def a_reported(session: requests.Session, period: str) -> int:
    """某报告期已披露现金流量表的 A 股公司数（用来判断最新可用的报告期）。"""
    return count(session, DC, reportName="RPT_F10_FINANCE_GCASHFLOW", columns="SECUCODE", filter=f"(REPORT_DATE='{period}')" + A_TYPE)


def a_forecasts(session: requests.Session, since: date) -> list[dict]:
    """业绩预告（最近披露的），含预告类型（预增 / 扭亏 / 预减…）与增幅区间。"""
    return rows_all(session, DC_WEB, "-NOTICE_DATE,SECUCODE,REPORT_DATE,PREDICT_FINANCE_CODE", reportName="RPT_PUBLIC_OP_NEWPREDICT",
                    columns="SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,NOTICE_DATE,REPORT_DATE,PREDICT_FINANCE_CODE,PREDICT_FINANCE,PREDICT_TYPE,"
                            "ADD_AMP_LOWER,ADD_AMP_UPPER,PREDICT_CONTENT,IS_LATEST",
                    filter=f"(NOTICE_DATE>='{since:%Y-%m-%d}')", max_pages=20)


# ---------------------------------------------------------------- 前瞻日历：分红、解禁、定期报告预约披露

def hk_dividends(session: requests.Session, codes: list[str], since: str) -> dict[str, list[dict]]:
    """港股分红记录（期间、除净日、派息日、进度、方案文字），每家按公告日倒序。"""
    out: dict[str, list[dict]] = defaultdict(list)
    for chunk in _chunks(codes):
        for r in rows_all(session, DC, "SECUCODE,NOTICE_DATE,ASSIGN_PERIOD,EX_DIVIDEND_DATE", reportName="RPT_HKF10_INFO_DIVIDEND",
                          columns="SECUCODE,NOTICE_DATE,ASSIGN_PERIOD,EX_DIVIDEND_DATE,DIVIDEND_DATE,PLAN_EXPLAIN,ASSIGN_PROGRESS,IS_DIVIDEND",
                          filter=_in("SECUCODE", [f"{c}.HK" for c in chunk]) + f"(NOTICE_DATE>='{since}')"):
            out[hk_code(r["SECUCODE"])].append(r)
    for rows in out.values():
        rows.sort(key=lambda r: r.get("NOTICE_DATE") or "", reverse=True)
    return dict(out)


def a_dividends(session: requests.Session, period: str) -> list[dict]:
    """某报告期的 A 股分红方案（每 10 股派息、预案 / 实施、除息日）。"""
    return rows_all(session, DC_WEB, "SECUCODE,NOTICE_DATE,EX_DIVIDEND_DATE", reportName="RPT_SHAREBONUS_DET",
                    columns="SECUCODE,SECURITY_CODE,REPORT_DATE,PRETAX_BONUS_RMB,PLAN_NOTICE_DATE,NOTICE_DATE,EX_DIVIDEND_DATE,"
                            "ASSIGN_PROGRESS,IMPL_PLAN_PROFILE",
                    filter=f"(REPORT_DATE='{period}')")


def a_lifts(session: requests.Session, start: date, end: date) -> list[dict]:
    """区间内的限售股解禁（解禁股数占总股本 / 流通股的比例、股份类型）。"""
    return rows_all(session, DC_WEB, "FREE_DATE,SECUCODE,FREE_SHARES_TYPE", reportName="RPT_LIFT_STAGE",
                    columns="SECUCODE,SECURITY_CODE,FREE_DATE,FREE_SHARES_TYPE,TOTAL_RATIO,FREE_RATIO,LIFT_MARKET_CAP",
                    filter=f"(FREE_DATE>='{start:%Y-%m-%d}')(FREE_DATE<='{end:%Y-%m-%d}')")


def a_appointments(session: requests.Session, period: str) -> list[dict]:
    """某报告期的预约披露日（含变更）与是否已披露。"""
    return rows_all(session, DC_WEB, "SECUCODE", reportName="RPT_PUBLIC_BS_APPOIN",
                    columns="SECUCODE,SECURITY_CODE,REPORT_DATE,REPORT_TYPE_NAME,APPOINT_PUBLISH_DATE,ACTUAL_PUBLISH_DATE,IS_PUBLISH",
                    filter=f"(REPORT_DATE='{period}')")
