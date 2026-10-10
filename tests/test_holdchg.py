"""大股东 / 董监高持股变动：港交所权益披露（DI）解析与判定、美股 Form 4 / 13D / 13G、状态与去重。全程离线。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from stockagent import holdchg
from stockagent.app import SOURCE_LABEL, fetch
from stockagent.sources import hkdi, nasdaq, sec
from stockagent.triage import TRIAGE_SYSTEM
from stockagent.utils import today_local

FIX = Path(__file__).parent / "fixtures"
REAL_NOTICES, REAL_SCHEDULE13 = hkdi.notices, sec.schedule13  # conftest 的 autouse 夹具会把它们换成离线桩；测原函数要在导入时留一份


def notice(serial="CS20261009E00025", name="Fund A", reasons=("1101",), involved=100_000.0, after=5_000_000.0, pct=5.0, day="2026-10-06",
           kind=None, price=None, currency=None, short=None):
    return {"serial": serial, "name": name, "kind": kind or ("director" if serial.startswith("D") else "holder"),
            "reasons": list(reasons), "involved": {"L": involved}, "currency": currency, "price": price,
            "after": {"L": after, **({"S": short} if short else {})}, "pct": {"L": pct}, "date": day,
            "url": f"https://di.hkex.com.hk/di/NSForm2.aspx?fn={serial}"}


# ---------------------------------------------------------------- 港交所 DI 解析

def test_parse_all_notices_page_uses_innermost_rows():
    rows, total = hkdi.parse((FIX / "hkdi_all_notices.html").read_text(encoding="utf-8"))
    assert total == 91 and len(rows) == 7  # 页面是嵌套表格，外层 tr 不能把行重复算
    assert len({r["serial"] for r in rows}) == 7
    first = rows[0]
    assert first["serial"] == "CS20261009E00025" and first["kind"] == "holder" and first["name"] == "BlackRock, Inc."
    assert first["reasons"] == ["1314"] and first["involved"] == {"L": 1720100.0}
    assert first["after"] == {"L": 90213433.0, "S": 3510648.0} and first["pct"] == {"L": 5.75, "S": 0.22}
    assert first["date"] == "2026-10-06" and first["url"] == "https://di.hkex.com.hk/di/NSForm2.aspx?fn=CS20261009E00025"
    jp = next(r for r in rows if r["name"].startswith("JPMorgan"))
    assert jp["reasons"] == ["10035", "1403"] and jp["currency"] == "HKD" and jp["price"] == pytest.approx(37.7897)
    assert jp["after"].get("P") == 26171286.0


def test_parse_director_row():
    rows, total = hkdi.parse((FIX / "hkdi_director.html").read_text(encoding="utf-8"))
    assert total == 1 and len(rows) == 1
    n = rows[0]
    assert n["kind"] == "director" and n["serial"] == "DA20260819E00389" and n["reasons"] == ["1316"]
    assert n["involved"] == {"L": 1682.0} and n["price"] is None and n["date"] == "2026-08-18"


def test_reason_codes():
    assert hkdi.reason_kind("1101") == hkdi.reason_kind("1001") == hkdi.reason_kind("1110") == hkdi.KIND_BUY
    assert hkdi.reason_kind("1201") == hkdi.reason_kind("1209") == hkdi.KIND_SELL
    assert hkdi.reason_kind("1305") == hkdi.reason_kind("1307") == hkdi.KIND_PLEDGE
    assert hkdi.reason_kind("1313") == hkdi.reason_kind("1402") == hkdi.KIND_LEND
    assert hkdi.reason_kind("1316") == hkdi.reason_kind("1102") == hkdi.reason_kind("99999") == hkdi.KIND_OTHER
    assert hkdi.reason_label("1101") == "买入" and hkdi.reason_label("1201") == "卖出" and hkdi.reason_label("1305") == "将股份质押给非合资格贷款人"
    assert hkdi.reason_label("1704") == "不再持有须披露的权益" and hkdi.reason_label("55555") == "55555"


def test_notices_pages_until_total(monkeypatch):
    pages = {"1": ([notice(f"CS2026100{i}E1") for i in range(50)], 70), "2": ([notice(f"CS2026101{i}E1") for i in range(20)], 70)}
    calls = []

    class S:
        def get(self, url, params=None, timeout=None):
            calls.append(params["pg"])
            return type("R", (), {"text": params["pg"], "raise_for_status": lambda s: None})()

    monkeypatch.setattr(hkdi, "parse", lambda text: pages[text])
    rows = REAL_NOTICES(S(), "09868", 1, date(2026, 8, 1), date(2026, 10, 10))
    assert len(rows) == 70 and calls == ["1", "2"]


# ---------------------------------------------------------------- 港股判定

def test_lending_collateral_and_other_events_never_report():
    ns = [notice(f"CS2026100{i}E1", reasons=("1314",), pct=5.6 + 0.01 * i, involved=200_000_000.0, after=560_000_000.0) for i in range(5)]
    assert holdchg.judge_hk(ns, {}, 0.5, 1e6, None) is None  # 证券借贷记 0，再大也不算买卖
    # 抵押品方（贝莱德常见）：取得 / 解除担保权益不是买卖
    coll = [notice("CS2A", reasons=("1004",), involved=9e7), notice("CS2B", reasons=("1205",), involved=9e7), notice("CS2C", reasons=("1104",), involved=9e7)]
    assert holdchg.judge_hk(coll, {}, 0.5, 1e6, None) is None
    # 「其他事项」：A/B 类股转换、IPO 前后的持股安排，涉及股数很大也不算
    other = [notice("DA20260924E593", name="Founder", reasons=("1113",), involved=3_510_027.0, after=112_587_152.0, pct=3.09),
             notice("DA20260924E592", name="Founder", reasons=("1213",), involved=3_510_027.0, after=320_581_472.0, pct=48.62)]
    assert holdchg.judge_hk(other, {}, 0.5, 1e6, None) is None
    # 明确的买入、获配、卖出、配售各自带方向；同一批里等量买卖互相抵消
    assert holdchg.implied_change(notice(reasons=("1101",), involved=1e6, after=5e7, pct=5.0)) == pytest.approx(0.1)
    assert holdchg.implied_change(notice(reasons=("1110",), involved=1e6, after=5e7, pct=5.0)) == pytest.approx(0.1)
    assert holdchg.implied_change(notice(reasons=("1209",), involved=1e6, after=5e7, pct=5.0)) == pytest.approx(-0.1)
    assert holdchg.implied_change(notice(reasons=("1101", "1201"), involved=1e6)) == 0.0  # 一份申报里买卖并列，方向不明


def test_net_buy_sell_accumulates_and_reports():
    res = holdchg.judge_hk([notice(reasons=("1101",), involved=6_000_000.0, after=62_000_000.0, pct=6.2)], {}, 0.5, 1e6, None)
    assert res and res["why"] == ["drift"] and res["acc"]["pct"] == pytest.approx(0.6) and res["after"] == 6.2
    res = holdchg.judge_hk([notice(reasons=("1201",), involved=6_000_000.0, after=58_000_000.0, pct=5.8)], {}, 0.5, 1e6, None)
    assert res and res["acc"]["pct"] == pytest.approx(-0.6)
    assert holdchg.judge_hk([notice(reasons=("1101",), involved=100_000.0)], {}, 0.5, 1e6, None) is None  # 0.1 个百分点，没过线
    assert holdchg.judge_hk([notice(reasons=("1101",), involved=100_000.0)], {"pct": 0.45, "gross": 0.45, "n": 2, "val": 0}, 0.5, 1e6, None)["why"] == ["drift"]  # 加上之前攒的 0.45 过线


def test_director_value_uses_price_or_reference_close():
    buy = notice("DA20261001E1", reasons=("1101",), involved=200_000.0, price=10.0, currency="HKD", pct=0.01)
    res = holdchg.judge_hk([buy], {}, 0.5, 1_000_000, None)
    assert res and res["why"] == ["value"] and res["value"] == {"buy": pytest.approx(2_000_000)}
    sell = notice("DA20261002E1", reasons=("1201",), involved=100_000.0, pct=0.01)  # 没有成交价，按最近收盘价 12 估算
    res = holdchg.judge_hk([sell], {}, 0.5, 1_000_000, 12.0)
    assert res and res["value"] == {"sell": pytest.approx(1_200_000)}
    assert holdchg.judge_hk([sell], {}, 0.5, 1_000_000, None) is None  # 没价格就估不出金额
    small = notice("DA20261003E1", reasons=("1101",), involved=1_000.0, price=10.0, currency="HKD", pct=0.01)
    assert holdchg.judge_hk([small], {}, 0.5, 1_000_000, None) is None
    # 大股东（非董事）买卖只看比例，不看金额
    assert holdchg.judge_hk([notice("CS20261001E1", reasons=("1101",), involved=1e6, after=5e7, price=10.0, currency="HKD", pct=5.0)], {}, 0.5, 1e6, None) is None


def test_pledge_by_director_or_large_block():
    assert holdchg.judge_hk([notice("DA20261001E1", reasons=("1305",), involved=500.0, pct=0.01)], {}, 0.5, 1e6, None)["why"] == ["pledge"]  # 董事质押，不论多少
    big = notice("CS20261001E1", reasons=("1305",), involved=20_000_000.0, after=100_000_000.0, pct=10.0)  # 已发行 10 亿股，质押 2%
    assert holdchg.judge_hk([big], {}, 0.5, 1e6, None)["why"] == ["pledge"]
    small = notice("CS20261001E2", reasons=("1305",), involved=1_000_000.0, after=100_000_000.0, pct=10.0)  # 0.1%
    assert holdchg.judge_hk([small], {}, 0.5, 1e6, None) is None


def test_streak_of_small_same_direction_buys_reports():
    """控股股东每天小额增持：单日 0.05 个百分点，连续 6 份攒到 0.3（阈值 0.5 的一半以上）就报。"""
    ns = [notice(f"CS2026100{i}E1", reasons=("1101",), involved=500_000.0, after=100_000_000.0 + i, pct=10.0, price=60.0, currency="HKD",
                 day=f"2026-10-0{i + 1}") for i in range(6)]  # 已发行 10 亿股，每份 0.05
    res = holdchg.judge_hk(ns, {}, 0.5, 1e6, None, streak=5)
    assert res and res["why"] == ["streak"] and res["acc"]["n"] == 6 and res["acc"]["pct"] == pytest.approx(0.3) and res["acc"]["val"] == 180_000_000
    assert holdchg.judge_hk(ns[:4], {}, 0.5, 1e6, None, streak=5) is None  # 才 4 份
    mixed = ns[:3] + [notice(f"CS2026101{i}E1", reasons=("1201",), involved=400_000.0, after=100_000_000.0, pct=10.0) for i in range(3)]
    assert holdchg.judge_hk(mixed, {}, 0.5, 1e6, None, streak=5) is None  # 来回买卖，净额不到总额的 80%


def test_superseded_notice_is_dropped_and_serial_cleaned():
    row = (FIX / "hkdi_director.html").read_text(encoding="utf-8")
    amended = row.replace(">DA20260819E00389</a>", ">DA20260819E00389(Superseded byDA20260819E00400)</a>")
    rows, _ = hkdi.parse(amended)
    assert rows[0]["serial"] == "DA20260819E00389" and rows[0]["superseded"] is True
    rows, _ = hkdi.parse(row.replace(">DA20260819E00389</a>", ">DA20260819E00400(Amendment toDA20260819E00389)</a>"))
    assert rows[0]["serial"] == "DA20260819E00400" and rows[0]["superseded"] is False


# ---------------------------------------------------------------- 港股抓取：状态、基线、去重

def _acme_hk(app):
    c = app.watchlist.get("acme")
    return c, next(l for l in c.listings if l.market == "HK")


def test_fetch_hk_accumulates_across_runs_then_reports_once(app, monkeypatch):
    c, l = _acme_hk(app)
    day = (today_local() - timedelta(days=2)).isoformat()
    lend = notice("CS20260901E1", name="Fund A", reasons=("1314",), pct=5.60, day=day)
    tiny = notice("DA20260902E1", name="Tiny Dir", kind="director", reasons=("1316",), involved=10.0, pct=0.0, day=day)
    monkeypatch.setattr(hkdi, "notices", lambda *a, **k: [lend, tiny])
    msgs = holdchg.fetch_hk(app, c, l)
    assert "2 份新申报" in msgs[0] and "新增持股变动 0" in msgs[0]
    assert not app.db._all("SELECT 1 FROM items WHERE source='holdchg'")
    st = json.loads(app.db.kv_get(f"holdchg:{l.key}"))
    assert st["acc"] == {} and set(st["done"]) == {"CS20260901E1", "DA20260902E1"}

    buy1 = notice("CS20260903E2", name="Fund A", reasons=("1101",), involved=3_000_000.0, pct=6.30, after=63_000_000.0, day=day)  # 0.3 个百分点
    monkeypatch.setattr(hkdi, "notices", lambda *a, **k: [lend, tiny, buy1])
    assert "新增持股变动 0" in holdchg.fetch_hk(app, c, l)[0]
    assert json.loads(app.db.kv_get(f"holdchg:{l.key}"))["acc"]["Fund A"]["pct"] == pytest.approx(0.3)

    buy2 = notice("CS20260904E3", name="Fund A", reasons=("1101",), involved=3_000_000.0, pct=6.60, after=66_000_000.0, day=day)  # 累计 0.6
    monkeypatch.setattr(hkdi, "notices", lambda *a, **k: [lend, tiny, buy1, buy2])
    msgs = holdchg.fetch_hk(app, c, l)
    assert "1 份新申报" in msgs[0] and "新增持股变动 1" in msgs[0]
    (it,) = app.db._all("SELECT * FROM items WHERE source='holdchg'")
    assert it["kind"] == "data" and it["snippet"].startswith(holdchg.TAG) and it["listing"] == l.key
    assert "大股东 Fund A" in it["title"] and "累计净买入约 0.60 个百分点" in it["title"] and "好仓现 6.60%" in it["title"]
    assert "买入（1101）" in it["snippet"] and "CS20260904E3" in it["snippet"]
    assert json.loads(app.db.kv_get(f"holdchg:{l.key}"))["acc"] == {}  # 报过就清零

    holdchg.fetch_hk(app, c, l)  # 同一批再跑：已处理的不重复
    assert len(app.db._all("SELECT 1 FROM items WHERE source='holdchg'")) == 1


def test_fetch_hk_source_down_and_missing_sid(app, monkeypatch):
    c, l = _acme_hk(app)
    monkeypatch.setattr(hkdi, "lookup_sid", lambda *a, **k: None)
    assert "找不到港交所权益披露系统里的公司编号" in holdchg.fetch_hk(app, c, l)[0]
    monkeypatch.setattr(hkdi, "lookup_sid", lambda *a, **k: 7)
    monkeypatch.setattr(hkdi, "notices", lambda *a, **k: (_ for _ in ()).throw(__import__("requests").ConnectionError("down")))
    assert holdchg.fetch_hk(app, c, l) == []  # 连接失败：熔断，不报错
    assert "hkex_di" in app.down


def test_fetch_wired_into_app_fetch(app, monkeypatch):
    seen = []
    monkeypatch.setattr(holdchg, "fetch", lambda app_, companies: seen.append([c.id for c in companies]) or [])
    fetch(app, [app.watchlist.get("acme")], prices=False, filings=False, news=False, market=True)
    assert seen == [["acme"]]
    seen.clear()
    fetch(app, [app.watchlist.get("acme")], prices=False, filings=False, news=False, market=False)  # 盘中 alert 不查
    assert seen == []
    app.settings.sources.holdchg = False
    fetch(app, [app.watchlist.get("acme")], prices=False, filings=False, news=False, market=True)
    assert seen == []


def test_hk_recent_text_summarises(app, monkeypatch):
    _, l = _acme_hk(app)
    rows = [notice("CS20261001E1", name="Fund A", reasons=("1314",), pct=5.7, day="2026-10-01"),
            notice("DA20261002E1", name="Boss Li", reasons=("1201",), involved=50_000.0, pct=0.2, after=2_000_000.0, price=9.5, currency="HKD", day="2026-10-02")]
    monkeypatch.setattr(hkdi, "notices", lambda *a, **k: rows)
    text = holdchg.hk_recent_text(app, l)
    assert "Fund A" in text and "Boss Li" in text and "卖出" in text and "借贷类多为被动基金" in text
    monkeypatch.setattr(hkdi, "notices", lambda *a, **k: [])
    assert holdchg.hk_recent_text(app, l) == ""


# ---------------------------------------------------------------- 美股

def f4(name, day, kind, shares, price, held=100_000.0, relation="Officer"):
    return {"name": name, "relation": relation, "date": day, "type": kind, "shares": shares, "price": price, "held": held}


def test_us_insider_events_filter_and_cluster():
    rows = [f4("A CEO", "10/01/2026", "Sell", 3000, 200.0, held=97_000),
            f4("A CEO", "10/01/2026", "Sell", 2000, 200.0, held=95_000),             # 同日同方向合并 → 100 万美元
            f4("B CFO", "09/20/2026", "Automatic Sell", 5000, 200.0),                  # 10b5-1，同样 100 万
            f4("C DIR", "09/25/2026", "Sell", 100, 200.0),                              # 金额不够，但算进集中卖出人数
            f4("D DIR", "09/30/2026", "Option Execute", 90_000, 10.0),                  # 期权行权不算
            f4("E DIR", "09/30/2026", "Acquisition (Non Open Market)", 90_000, 300.0),  # 授予不算
            f4("F DIR", "09/30/2026", "Buy", 3000, 200.0, relation="Director")]        # 买入 60 万
    ev = holdchg.us_insider_events(rows, date(2026, 9, 1), 500_000)
    assert [(g["name"], g["side"], g["auto"]) for g in ev] == [("B CFO", "sell", True), ("F DIR", "buy", False), ("A CEO", "sell", False)]
    ceo = ev[2]
    assert ceo["shares"] == 5000 and ceo["value"] == pytest.approx(1_000_000) and ceo["cluster"] == 3  # A CEO、B CFO、C DIR
    assert ev[1]["cluster"] == 1
    assert [g["name"] for g in holdchg.us_insider_events(rows, date(2026, 9, 28), 500_000)] == ["F DIR", "A CEO"]  # 水位线之前的不报


def test_fetch_us_insider_items_dedupe(app, monkeypatch):
    c = app.watchlist.get("acme")
    us = type(c.listings[0])(market="US", code="ACME", exchange="NYSE")
    today = today_local()
    day = (today - timedelta(days=3)).strftime("%m/%d/%Y")
    rows = [f4("A CEO", day, "Automatic Sell", 5000, 200.0, held=95_000)]
    monkeypatch.setattr(nasdaq, "insiders", lambda *a, **k: {"summary": [], "recent": rows})
    msgs = holdchg.fetch_us(app, c, us)
    assert "新增持股变动 1" in msgs[0]
    (it,) = app.db._all("SELECT * FROM items WHERE source='holdchg'")
    assert it["title"].startswith("内部人 A CEO（Officer）公开市场卖出 5,000 股") and "10b5-1" in it["snippet"] and it["snippet"].startswith(holdchg.TAG)
    assert "占其持股的 5.0%" in it["snippet"]
    assert holdchg.fetch_us(app, c, us) == []  # 再跑：已处理
    assert len(app.db._all("SELECT 1 FROM items WHERE source='holdchg'")) == 1


def info(name, pct, shares=1_000_000.0, amendment="3"):
    return {"form": "x", "amendment": amendment, "event_date": "06/30/2026", "issuer": "Acme", "persons": [{"name": name, "percent": pct, "shares": shares}]}


def test_schedule13_rules():
    levels: dict = {}
    assert holdchg.schedule13_event(info("Activist", 7.0), "SCHEDULE 13D", levels, 1.0)["reason"] == "13D"
    assert holdchg.schedule13_event(info("Activist", 8.5), "SCHEDULE 13D/A", levels, 1.0)["prev"] == 7.0
    assert holdchg.schedule13_event(info("Fund", 5.3), "SCHEDULE 13G", levels, 1.0)["reason"] == "new"
    assert holdchg.schedule13_event(info("Fund", 5.8), "SCHEDULE 13G/A", levels, 1.0) is None            # +0.5，没过线
    assert holdchg.schedule13_event(info("Fund", 7.0), "SCHEDULE 13G/A", levels, 1.0)["reason"] == "change"  # 5.8 → 7.0
    assert holdchg.schedule13_event(info("Fund", 4.9), "SCHEDULE 13G/A", levels, 1.0)["reason"] == "change"  # 降到 5% 以下
    assert holdchg.schedule13_event(info("Newbie", 6.0), "SCHEDULE 13G/A", levels, 1.0) is None               # 没有基线，只记录
    assert levels["Newbie"] == 6.0
    assert holdchg.schedule13_event(None, "SCHEDULE 13D/A", levels, 1.0) == {"reason": "unparsed"}
    assert holdchg.schedule13_event(None, "SCHEDULE 13G/A", levels, 1.0) is None


def test_fetch_us_schedule13(app, monkeypatch):
    app.settings.sources.sec_user_agent = "tester test@example.com"
    c = app.watchlist.get("acme")
    us = type(c.listings[0])(market="US", code="ACME", exchange="NYSE")
    monkeypatch.setattr(nasdaq, "insiders", lambda *a, **k: None)
    monkeypatch.setattr(holdchg.ids, "sec_cik", lambda *a, **k: 123)
    now = datetime.now(timezone.utc)
    filings = [{"source_id": "0001-26-000001", "title": "SCHEDULE 13D/A", "category": "SCHEDULE 13D/A", "url": "https://sec/x/primary_doc.xml",
                "published_at": now - timedelta(days=1)}]
    monkeypatch.setattr(sec, "filings", lambda *a, **k: filings)
    monkeypatch.setattr(sec, "schedule13", lambda *a, **k: info("Cohen Ryan", 8.9, 45_383_306.0, "20"))
    assert "新增持股变动 1" in holdchg.fetch_us(app, c, us)[0]
    (it,) = app.db._all("SELECT * FROM items WHERE source='holdchg'")
    assert "Cohen Ryan 披露持有" in it["title"] and "8.90%" in it["title"] and "第 20 号申报" in it["snippet"] and it["source_name"] == "SCHEDULE 13D/A"
    assert holdchg.fetch_us(app, c, us) == []


def test_sec_schedule13_xml(monkeypatch):
    xml = """<?xml version="1.0"?><edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13g">
      <headerData><submissionType>SCHEDULE 13G/A</submissionType></headerData>
      <formData><coverPageHeader><amendmentNo>10</amendmentNo><eventDateRequiresFilingThisStatement>06/30/2026</eventDateRequiresFilingThisStatement>
        <issuerInfo><issuerName>Bloom Energy Corp</issuerName></issuerInfo></coverPageHeader>
        <coverPageHeaderReportingPersonDetails><reportingPersonName>Ameriprise Financial, Inc.</reportingPersonName>
          <reportingPersonBeneficiallyOwnedAggregateNumberOfShares>18231962</reportingPersonBeneficiallyOwnedAggregateNumberOfShares>
          <classPercent>6.4</classPercent></coverPageHeaderReportingPersonDetails>
        <coverPageHeaderReportingPersonDetails><reportingPersonName>Columbia Management</reportingPersonName>
          <reportingPersonBeneficiallyOwnedAggregateNumberOfShares>16000000</reportingPersonBeneficiallyOwnedAggregateNumberOfShares>
          <classPercent>5.6</classPercent></coverPageHeaderReportingPersonDetails></formData></edgarSubmission>"""
    got = {}

    class S:
        def get(self, url, headers=None, timeout=None):
            got["url"] = url
            return type("R", (), {"content": xml.encode(), "raise_for_status": lambda s: None})()

    r = REAL_SCHEDULE13(S(), "https://www.sec.gov/Archives/edgar/data/1/0001/xslSCHEDULE_13G_X02/primary_doc.xml", "ua")
    assert got["url"].endswith("/0001/primary_doc.xml")  # 换成原始 XML
    assert r["form"] == "SCHEDULE 13G/A" and r["amendment"] == "10" and r["event_date"] == "06/30/2026" and r["issuer"] == "Bloom Energy Corp"
    assert [(p["name"], p["percent"], p["shares"]) for p in r["persons"]] == [("Ameriprise Financial, Inc.", 6.4, 18231962.0), ("Columbia Management", 5.6, 16000000.0)]
    S.get = lambda self, *a, **k: type("R", (), {"content": b"not xml", "raise_for_status": lambda s: None})()
    assert REAL_SCHEDULE13(S(), "https://x/primary_doc.xml", "ua") is None


# ---------------------------------------------------------------- 与分级、工具的衔接

def test_triage_knows_holdchg():
    assert SOURCE_LABEL["holdchg"] == "持股变动" and "「[持股变动]」" in TRIAGE_SYSTEM and "insider_trade" in TRIAGE_SYSTEM


def test_shareholders_tool_appends_hk_di(app, monkeypatch):
    from stockagent import tools
    from stockagent.sources import emdata

    c, l = _acme_hk(app)
    monkeypatch.setattr(emdata, "hk_holders", lambda *a, **k: [])
    monkeypatch.setattr(tools, "_a_holders_text", lambda *a, **k: "A 股股东（桩）")  # acme 同时有 A 股上市地，工具会一并查询
    monkeypatch.setattr(holdchg, "hk_recent_text", lambda *a, **k: "DI 概览：Boss Li 卖出")
    out = tools.ToolBox(app, c).shareholders()
    assert "A 股股东（桩）" in out and "没有主要股东披露数据" in out and "DI 概览：Boss Li 卖出" in out
