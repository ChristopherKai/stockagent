"""港交所「中央结算系统持股纪录」（CCASS）：某日各参与者（券商、托管行、港股通）在中央结算系统中持有某只港股的数量。

页面是 ASP.NET 表单：先 GET 取 __VIEWSTATE 等隐藏字段，再 POST 查询。只能查最近 12 个月；数据为 T 日日终，T+1 日可查。
港交所声明该数据供个人及非商业用途，这里只按自选股每天查一次。
"""

from __future__ import annotations

import re
import time
from datetime import date

import requests
from bs4 import BeautifulSoup

URL = "https://www3.hkexnews.hk/sdw/search/searchsdw_c.aspx"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/124.0 Safari/537.36", "Referer": URL, "Origin": "https://www3.hkexnews.hk"}
# 港股通的名义持有人（中国结算：A00003 沪港通、A00004 深港通）；南向资金另有 southbound 数据
CONNECT_IDS = {"A00003": "港股通（沪）", "A00004": "港股通（深）"}
SUMMARY = {"市場中介者": "intermediaries", "願意披露的投資者戶口持有人": "investors_disclosed",
           "不願意披露的投資者戶口持有人": "investors_undisclosed", "總數": "total"}


def _int(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _pct(text: str) -> float | None:
    m = re.search(r"-?[\d.]+", text or "")
    return float(m.group()) if m else None


def _cell(tr, cls: str) -> str:
    td = tr.find("td", class_=cls)
    if td is None:
        return ""
    body = td.find("div", class_="mobile-list-body")
    return (body or td).get_text(strip=True)


def parse(html: str) -> dict | None:
    """查询结果页 → {"date", "issued", "summary": {类别: {shares, participants, pct}}, "participants": [{id, name, shares, pct}]}。
    没有结果（代码不存在、日期超出范围）返回 None。"""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        pid = _cell(tr, "col-participant-id")
        if not pid:
            continue
        rows.append({"id": pid, "name": _cell(tr, "col-participant-name"),
                     "shares": _int(_cell(tr, "col-shareholding")) or 0, "pct": _pct(_cell(tr, "col-shareholding-percent"))})
    if not rows:
        return None
    summary = {}
    for row in soup.find_all("div", class_="ccass-search-datarow"):
        cat = row.find("div", class_="summary-category")
        key = SUMMARY.get(cat.get_text(strip=True)) if cat else None
        if not key:
            continue
        vals = [v.get_text(strip=True) for v in row.find_all("div", class_="value")]
        if len(vals) >= 3:
            summary[key] = {"shares": _int(vals[0]), "participants": _int(vals[1]), "pct": _pct(vals[2])}
    issued = soup.find("div", class_="summary-value")
    day = soup.find("input", {"name": "txtShareholdingDate"})
    return {"date": (day.get("value") or "").replace("/", "-") if day else None,
            "issued": _int(issued.get_text()) if issued else None,
            "summary": summary, "participants": sorted(rows, key=lambda r: r["shares"], reverse=True)}


_form: dict = {}  # 上一次查询页的隐藏字段（__VIEWSTATE 等），短时间内复用可省掉一次 GET（港交所这个页面每次请求要 10 秒左右）
FORM_TTL = 600


def _fresh_form(session: requests.Session) -> dict:
    r = session.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return _hidden(r.text)


def _hidden(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    form = {i["name"]: i.get("value", "") for i in soup.find_all("input", {"type": "hidden"}) if i.get("name")}
    if "__VIEWSTATE" not in form:
        raise ValueError("港交所 CCASS 页面结构变化：找不到 __VIEWSTATE")
    return form


def holdings(session: requests.Session, code: str, day: date) -> dict | None:
    """查询 code（港股代码）在 day 日终的 CCASS 持股；非交易日港交所按前一交易日返回（结果里的 date 为实际日期）。"""
    cached = _form.get("fields") if time.time() - _form.get("at", 0) < FORM_TTL else None
    for form in ([cached] if cached else []) + [None]:
        form = dict(form or _fresh_form(session))
        form.update({"__EVENTTARGET": "btnSearch", "__EVENTARGUMENT": "", "sortBy": "shareholding", "sortDirection": "desc",
                     "txtShareholdingDate": day.strftime("%Y/%m/%d"), "txtStockCode": code.zfill(5), "txtStockName": "",
                     "txtParticipantID": "", "txtParticipantName": "", "txtSelPartID": ""})
        r = session.post(URL, data=form, headers=HEADERS, timeout=60)
        r.raise_for_status()
        result = parse(r.text)
        try:
            _form.update(fields=_hidden(r.text), at=time.time())
        except ValueError:
            _form.clear()
        if result is not None:
            return result
    return None  # 复用的表单和新表单都查不到：代码不存在或日期超出范围


def changes(old: dict, new: dict, min_pct: float) -> list[dict]:
    """两次快照之间持股比例变化 ≥ min_pct 个百分点的参与者（按变化绝对值倒序）。比例按新快照的已发行股数统一换算。"""
    issued = new.get("issued") or old.get("issued")
    if not issued:
        return []
    before = {p["id"]: p for p in old["participants"]}
    after = {p["id"]: p for p in new["participants"]}
    out = []
    for pid in before.keys() | after.keys():
        a, b = before.get(pid), after.get(pid)
        delta = (b["shares"] if b else 0) - (a["shares"] if a else 0)
        pct = delta / issued * 100
        if abs(pct) >= min_pct:
            out.append({"id": pid, "name": CONNECT_IDS.get(pid) or (b or a)["name"], "delta": delta, "pct": pct,
                        "after_pct": (b["shares"] if b else 0) / issued * 100})
    return sorted(out, key=lambda x: abs(x["pct"]), reverse=True)


def concentration(snap: dict, n: int) -> float | None:
    """前 n 名参与者合计占已发行股份的比例（%）；港股通两个账户视为一个。"""
    if not snap.get("issued"):
        return None
    merged: dict[str, int] = {}
    for p in snap["participants"]:
        key = "connect" if p["id"] in CONNECT_IDS else p["id"]
        merged[key] = merged.get(key, 0) + p["shares"]
    return sum(sorted(merged.values(), reverse=True)[:n]) / snap["issued"] * 100
