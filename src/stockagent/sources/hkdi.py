"""港交所披露易「权益披露」（DI，Disclosure of Interests）：持股 5% 以上的大股东和董事 / 最高行政人员的权益变动申报。

页面是普通 GET 的 ASP.NET 列表（不需要 VIEWSTATE）：先用「按上市法团查询」页拿到该公司的 DI 内部 id（sid，与披露易公告的 stockId 不同），
再取「所有申报列表」（NSAllFormList），每页 50 条，用 pg=N 翻页。2017-07 起的申报都在这里（DION 系统）。
Akamai 会拒绝非浏览器的 User-Agent（503），所以必须带浏览器 UA（app.session 默认就是）。
"""

from __future__ import annotations

import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

BASE = "https://di.hkex.com.hk/di"
PAGE_SIZE = 50
MAX_PAGES = 12

# 申报理由代码（ft=DA/CS/IS 的标准代码表）：前两位是大类，后两位是具体事项。代码以 NSStdCode 为准。
ACQUIRE = {  # 10xx / 11xx：取得权益
    "01": "买入", "02": "受赠", "03": "取得衍生工具权益", "04": "取得股份担保权益（接受质押）", "05": "继承", "06": "成为信托受益人",
    "07": "合资格贷款人执行担保权益", "09": "换股协议", "10": "作为承配人获配股份", "11": "先旧后新配售后获发新股", "12": "加入一致行动人集团", "13": "其他",
}
DISPOSE = {  # 12xx：减少权益
    "01": "卖出", "02": "赠出", "03": "交付股份或衍生工具款项", "04": "衍生工具到期或注销", "05": "不再持有担保权益", "06": "放弃或出售供股权利",
    "08": "换股协议", "09": "配售股份（先旧后新配售）", "10": "先旧后新配售中公司发行新股（持股被摊薄）", "11": "退出一致行动人集团", "13": "其他",
}
OTHER = {  # 13xx：其他事件
    "01": "此前未申报的买入股份已交收", "02": "签订出售协议", "03": "行使衍生工具权利", "04": "衍生工具权利被行使", "05": "将股份质押给非合资格贷款人",
    "06": "解除质押", "07": "对质押股份行使担保权益", "08": "质押股份被对方行使担保权益", "09": "遗嘱执行人转让股份", "10": "信托受托人转让股份",
    "11": "把股份交付给借入方", "12": "借出的股份被归还", "13": "借出股份（证券借贷）", "14": "收回借出股份", "15": "设立信托（仍持有股份）", "16": "其他事项",
}
SPECIAL = {  # 14xx–17xx
    "1401": "取得衍生工具", "1402": "借入股份", "1403": "其他", "1501": "衍生工具到期或注销", "1502": "归还借入股份", "1503": "其他",
    "1601": "证券借贷通知（认可借出代理人）", "1602": "证券借贷通知", "1701": "上市时申报", "1702": "更正过时资料", "1703": "披露门槛变化",
    "1704": "不再持有须披露的权益", "1705": "成为董事或最高行政人员", "1706": "不再担任董事或最高行政人员", "1710": "自愿披露", "1711": "其他",
}
KIND_BUY, KIND_SELL, KIND_PLEDGE, KIND_LEND, KIND_OTHER = "buy", "sell", "pledge", "lend", "other"
PLEDGE_CODES = {"1305", "1306", "1307", "1308"}
LEND_CODES = {"1311", "1312", "1313", "1314", "1402", "1502", "1601", "1602"}
BUY_CODES = {"01", "10"}   # 10xx / 11xx 的买入、获配
SELL_CODES = {"01", "09"}  # 12xx 的卖出、配售


def reason_label(code: str) -> str:
    """代码 → 中文含义；不认识的代码原样返回。"""
    if code in SPECIAL:
        return SPECIAL[code]
    head, tail = code[:2], code[2:]
    table = ACQUIRE if head in ("10", "11") else DISPOSE if head == "12" else OTHER if head == "13" else {}
    return table.get(tail, code)


def reason_kind(code: str) -> str:
    """代码 → buy / sell / pledge / lend / other。只有明确的买入、获配、卖出、配售才算买卖，质押与证券借贷单列。"""
    if code in PLEDGE_CODES:
        return KIND_PLEDGE
    if code in LEND_CODES:
        return KIND_LEND
    head, tail = code[:2], code[2:]
    if head in ("10", "11") and tail in BUY_CODES:
        return KIND_BUY
    if head == "12" and tail in SELL_CODES:
        return KIND_SELL
    return KIND_OTHER


def _num(text: str) -> float | None:
    m = re.search(r"-?[\d,]+(?:\.\d+)?", text or "")
    return float(m.group().replace(",", "")) if m else None


def _positions(cell) -> dict[str, float]:
    """「90,213,433(L)<br>3,510,648(S)」→ {"L": 90213433, "S": 3510648}（L 好仓 / S 淡仓 / P 借出池）。"""
    out = {}
    for m in re.finditer(r"(-?[\d,]+(?:\.\d+)?)\s*\(([LSP])\)", cell.get_text(" ", strip=True)):
        out[m.group(2)] = float(m.group(1).replace(",", ""))
    return out


def _reasons(cell) -> list[str]:
    """理由列：每个代码一个 <a>，后面跟 (L)/(S)；只取代码。"""
    return [a.get_text(strip=True) for a in cell.find_all("a") if re.fullmatch(r"\d{4,5}", a.get_text(strip=True))]


def _price(text: str) -> tuple[str | None, float | None]:
    """「HKD 37.7897」→ ("HKD", 37.7897)；空白 → (None, None)。"""
    m = re.search(r"([A-Z]{3})\s*([\d,]+(?:\.\d+)?)", text or "")
    return (m.group(1), float(m.group(2).replace(",", ""))) if m else (None, None)


def parse(html: str) -> tuple[list[dict], int]:
    """所有申报列表的一页 → (行, 总记录数)。每行：serial, name, kind(director|holder), reasons, involved{L,S}, currency, price,
    after{L,S,P}, pct{L,S,P}, date（事件日）, url, superseded（已被修订版替代）。"""
    soup = BeautifulSoup(html, "html.parser")
    total = 0
    if m := re.search(r"Total records:\s*([\d,]+)", soup.get_text(" ", strip=True)):
        total = int(m.group(1).replace(",", ""))
    rows = []
    for tr in soup.find_all("tr"):
        if tr.find("tr") is not None:  # 页面是嵌套表格，外层 tr 会把内层的行再算一遍
            continue
        tds = tr.find_all("td", class_="tbCell")
        if len(tds) != 10:
            continue
        raw_serial = tds[0].get_text(strip=True)  # 被修订的申报显示为「CS…(Superseded by CS…)」，修订版显示「CS…(Amendment to CS…)」
        serial = (re.match(r"[A-Z]{2}\d{8}E\d+", raw_serial) or re.match(r".*", raw_serial)).group()
        try:
            day = datetime.strptime(tds[7].get_text(strip=True), "%d/%m/%Y").date()
        except ValueError:
            continue
        link = tds[0].find("a")
        cur, price = _price(tds[4].get_text(" ", strip=True))
        rows.append({
            "serial": serial, "superseded": "Superseded" in raw_serial, "name": tds[1].get_text(" ", strip=True),
            "kind": "director" if serial.startswith("D") else "holder",
            "reasons": _reasons(tds[2]), "involved": _positions(tds[3]), "currency": cur, "price": price,
            "after": _positions(tds[5]), "pct": _positions(tds[6]), "date": day.isoformat(),
            "url": f"{BASE}/{link['href'].split('&sa2=')[0]}" if link and link.get("href") else None,
        })
    return rows, total


def lookup_sid(session: requests.Session, code: str) -> int | None:
    """港股代码（5 位）→ DI 内部 id；代码不存在返回 None。"""
    today = date.today()
    r = session.get(f"{BASE}/NSSrchCorpList.aspx", params={
        "sa1": "cl", "scsd": f"01/01/{today.year}", "sced": today.strftime("%d/%m/%Y"), "sc": code, "src": "MAIN", "lang": "EN"}, timeout=30)
    r.raise_for_status()
    m = re.search(r"NSAllFormList\.aspx\?[^\"']*?sid=(\d+)", r.text)
    return int(m.group(1)) if m else None


def notices(session: requests.Session, code: str, sid: int, start: date, end: date, max_pages: int = MAX_PAGES) -> list[dict]:
    """[start, end] 内按事件日倒序的全部申报（含大股东和董事）。"""
    fmt = "%d/%m/%Y"
    params = {"sa2": "an", "sid": str(sid), "corpn": "", "sd": start.strftime(fmt), "ed": end.strftime(fmt), "cid": "0",
              "sa1": "cl", "scsd": start.strftime(fmt), "sced": end.strftime(fmt), "sc": code, "src": "MAIN", "lang": "EN"}
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        r = session.get(f"{BASE}/NSAllFormList.aspx", params={**params, "pg": str(page)}, timeout=30)
        r.raise_for_status()
        rows, total = parse(r.text)
        out += rows
        if not rows or len(out) >= total:
            break
    return [r for r in out if not r.get("superseded")]  # 被修订替代的旧版不算（修订版是另一份申报，会单独出现）
