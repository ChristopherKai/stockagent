"""完整报表（东方财富 / SEC XBRL）、股东、CCASS 持股、美股一致预期：解析、工具输出与每日 CCASS 抓取（不联网）。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from stockagent import app as appmod
from stockagent import tools
from stockagent.sources import ccass, emdata, nasdaq, sec
from stockagent.utils import today_local

FIXTURES = Path(__file__).parent / "fixtures"
HOLDINGS = ccass.holdings  # 收集阶段取原函数；conftest 的 autouse fixture 会把它替换成不联网的桩


# ---------------------------------------------------------------- 报表解析

def test_a_statement_keeps_known_items_in_order():
    rows = [{"REPORT_DATE": "2025-12-31 00:00:00", "OPERATE_INCOME": 100e8, "OPERATE_COST": 60e8, "RESEARCH_EXPENSE": 5e8,
             "BASIC_EPS": 1.2, "UNKNOWN_FIELD": 9},
            {"REPORT_DATE": "2026-06-30 00:00:00", "OPERATE_INCOME": 55e8, "OPERATE_COST": 30e8, "RESEARCH_EXPENSE": None, "BASIC_EPS": 0.7}]
    st = emdata.a_statement_from_rows("income", rows)
    assert [p["label"] for p in st["periods"]] == ["2026-06-30（6个月）", "2025-12-31（全年）"]
    assert [r[0] for r in st["rows"]] == ["营业收入", "营业成本", "研发费用", "基本每股收益"]
    assert st["rows"][2][1] == [None, 5e8]
    bal = emdata.a_statement_from_rows("balance", [{"REPORT_DATE": "2026-06-30 00:00:00", "MONETARYFUNDS": 1e8}])
    assert bal["periods"][0]["label"] == "2026-06-30"  # 时点数不标期间长度


def test_hk_statement_pivots_long_table():
    def row(d, start, code, name, amount):
        return {"REPORT_DATE": f"{d} 00:00:00", "START_DATE": f"{start} 00:00:00", "STD_ITEM_CODE": code,
                "STD_ITEM_NAME": name, "AMOUNT": amount}
    rows = [row("2026-06-30", "2026-01-01", "004007999", "毛利", 30), row("2026-06-30", "2026-01-01", "004001001", "营业额", 100),
            row("2026-06-30", "2026-01-01", "004099999", "非运算项目", 1), row("2025-12-31", "2025-01-01", "004001001", "营业额", 180),
            row("2024-12-31", "2024-01-01", "004001001", "营业额", 150)]
    st = emdata.hk_statement_from_rows("income", rows, periods=2)
    assert [p["label"] for p in st["periods"]] == ["2026-06-30（6个月）", "2025-12-31（全年）"]
    assert st["rows"] == [("营业额", [100, 180]), ("毛利", [30, None])]


def _fact(start, end, val, form="10-Q", filed="2026-08-01"):
    f = {"end": end, "val": val, "form": form, "filed": filed}
    if start:
        f["start"] = start
    return f


def test_sec_statement_picks_quarter_and_merges_concepts():
    gaap = {
        "Revenues": {"units": {"USD": [_fact("2024-01-01", "2024-12-31", 300, "10-K", "2025-02-01")]}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            _fact("2025-01-01", "2025-12-31", 400, "10-K", "2026-02-01"),
            _fact("2026-04-01", "2026-06-30", 110), _fact("2026-01-01", "2026-06-30", 210),  # 10-Q：单季与年初至今
            _fact("2026-04-01", "2026-06-30", 111, "10-Q/A", "2026-09-01"),  # 更正后的数字优先
        ]}},
        "EarningsPerShareDiluted": {"units": {"USD/shares": [_fact("2026-04-01", "2026-06-30", 0.5)]}},
        "Assets": {"units": {"USD": [_fact(None, "2025-12-31", 900, "10-K"), _fact(None, "2026-06-30", 950)]}},
        "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [_fact("2026-01-01", "2026-06-30", 80)]}},
    }
    facts = {"facts": {"us-gaap": gaap}}
    st = sec.statement_from_facts(facts, "income", 5)
    assert st["currency"] == "USD"
    assert [p["label"] for p in st["periods"]] == ["2026-06-30（3个月）", "2025-12-31（全年）", "2024-12-31（全年）"]
    assert st["rows"][0] == ("收入", [111, 400, 300])
    assert ("稀释每股收益", [0.5, None, None]) in st["rows"]
    assert [p["date"] for p in sec.statement_from_facts(facts, "income", 5, annual_only=True)["periods"]] == ["2025-12-31", "2024-12-31"]
    assert [p["date"] for p in sec.statement_from_facts(facts, "balance", 5, annual_only=True)["periods"]] == ["2025-12-31"]
    cf = sec.statement_from_facts(facts, "cashflow", 5)
    assert cf["periods"][0]["label"] == "2026-06-30（6个月）"  # 10-Q 的现金流量表只有年初至今


# ---------------------------------------------------------------- CCASS

def test_ccass_parse_fixture():
    d = ccass.parse((FIXTURES / "ccass_00700.html").read_text(encoding="utf-8"))
    assert d["date"] == "2026-09-29" and d["issued"] == 9_095_140_845
    assert d["summary"]["total"] == {"shares": 7_051_960_027, "participants": 828, "pct": 77.52}
    assert d["participants"][0] == {"id": "C00019", "name": "香港上海匯豐銀行有限公司", "shares": 2_954_037_020, "pct": 32.47}
    assert ccass.parse("<html><body>沒有紀錄</body></html>") is None


def test_ccass_reuses_form_and_retries_with_fresh_one(monkeypatch):
    result = '<input type="hidden" name="__VIEWSTATE" value="v2" />' + (FIXTURES / "ccass_00700.html").read_text(encoding="utf-8")
    page = '<input type="hidden" name="__VIEWSTATE" value="v1" /><input type="hidden" name="today" value="20260930" />'

    class Session:
        def __init__(self, posts):
            self.posts, self.calls = posts, []

        def get(self, url, **kw):
            self.calls.append("GET")
            return type("R", (), {"text": page, "raise_for_status": lambda self: None})()

        def post(self, url, data, **kw):
            self.calls.append(f"POST {data['__VIEWSTATE']}")
            return type("R", (), {"text": self.posts.pop(0), "raise_for_status": lambda self: None})()

    monkeypatch.setattr(ccass, "_form", {})
    s1 = Session([result])
    assert HOLDINGS(s1, "700", date(2026, 9, 29))["date"] == "2026-09-29" and s1.calls == ["GET", "POST v1"]
    s2 = Session([result])  # 结果页里的隐藏字段留作下次查询用，不再 GET
    HOLDINGS(s2, "700", date(2026, 9, 29))
    assert s2.calls == ["POST v2"]
    s3 = Session(["<html>過期</html>", "<html>沒有紀錄</html>"])  # 复用的表单失效：换新表单再查一次
    assert HOLDINGS(s3, "99999", date(2026, 9, 29)) is None and s3.calls[1:] == ["GET", "POST v1"]


def test_ccass_changes_and_concentration():
    old = {"issued": 1000, "participants": [{"id": "C1", "name": "甲", "shares": 300}, {"id": "C2", "name": "乙", "shares": 100},
                                           {"id": "A00003", "name": "中國結算", "shares": 50}]}
    new = {"issued": 1000, "participants": [{"id": "C1", "name": "甲", "shares": 200}, {"id": "C2", "name": "乙", "shares": 195},
                                           {"id": "A00003", "name": "中國結算", "shares": 52}, {"id": "B9", "name": "丙", "shares": 20}]}
    moves = ccass.changes(old, new, 1.0)
    assert [(m["id"], round(m["pct"], 1)) for m in moves] == [("C1", -10.0), ("C2", 9.5), ("B9", 2.0)]
    assert ccass.changes(old, new, 20) == []
    new["participants"].append({"id": "A00004", "name": "中國結算", "shares": 150})
    assert ccass.concentration(new, 2) == pytest.approx(40.2)  # 港股通两个账户合并为 202，加上 C1 的 200


def _snap(day: str, holdings: dict[str, int], issued: int = 1_000_000) -> dict:
    parts = [{"id": k, "name": f"參與者{k}", "shares": v, "pct": v / issued * 100} for k, v in holdings.items()]
    return {"date": day, "issued": issued, "summary": {"total": {"shares": sum(holdings.values()), "participants": len(parts), "pct": 70.0}},
            "participants": sorted(parts, key=lambda p: p["shares"], reverse=True)}


def test_fetch_ccass_first_snapshot_then_change_item(app, monkeypatch):
    c = app.watchlist.get("acme")
    hk = next(l for l in c.listings if l.market == "HK")
    today = today_local()
    d1, d2 = (today - timedelta(days=3)).isoformat(), (today - timedelta(days=1)).isoformat()
    queue = [_snap(d1, {"C1": 400_000, "C2": 300_000}), _snap(d2, {"C1": 370_000, "C2": 300_000, "B7": 5_000})]
    monkeypatch.setattr(ccass, "holdings", lambda session, code, day: queue.pop(0))

    msgs = appmod.fetch_ccass(app, c, hk)
    assert "首次记录" in msgs[0] and app.db.ccass("01234")["date"] == d1
    assert appmod.fetch_ccass(app, c, hk) == []  # 当天已查过
    app.db.kv_delete(f"ccass_checked:{hk.key}")
    msgs = appmod.fetch_ccass(app, c, hk)
    assert "变动 1 个参与者" in msgs[0]
    items = app.db.items_since(c.id, "2000-01-01T00:00:00+00:00")
    assert len(items) == 1 and items[0]["kind"] == "data" and items[0]["source"] == "ccass"
    assert "參與者C1" not in items[0]["title"] and "参与者C1 -3.00%" in items[0]["title"]  # 名称转为简体


# ---------------------------------------------------------------- 工具

def test_statements_tool_formats_and_prefers_a_share(app, monkeypatch):
    seen = []

    def fake(session, listing, kind, periods, annual_only):
        seen.append((listing.market, kind, periods, annual_only))
        return {"periods": [{"date": "2026-06-30", "label": "2026-06-30（6个月）"}],
                "rows": [("营业收入", [123.456e8]), ("基本每股收益", [0.85]), ("研发费用", [None])]}

    monkeypatch.setattr(emdata, "statement", fake)
    out = tools.ToolBox(app, app.watchlist.get("acme")).call("statements", {"kind": "income", "annual_only": True})
    assert seen == [("A", "income", 4, True)]
    assert "| 营业收入 | 123.46 |" in out and "| 基本每股收益 | 0.85 |" in out and "| 研发费用 | — |" in out
    assert out.startswith("300001.SZ 利润表")
    assert "错误" in tools.ToolBox(app, app.watchlist.get("acme")).call("statements", {"kind": "profit"})


def test_statements_tool_us_needs_sec_user_agent(app, monkeypatch):
    box = tools.ToolBox(app, app.watchlist.get("acme"))
    assert "sec_user_agent" in box.call("statements", {"kind": "income", "ticker": "NVDA"})
    app.settings.sources.sec_user_agent = "Test test@example.com"
    monkeypatch.setattr(tools.ids, "sec_cik", lambda *a: 1045810)
    monkeypatch.setattr(sec, "company_facts", lambda *a: {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        _fact("2025-02-01", "2026-01-31", 130e9, "10-K")]}}}}})
    out = box.call("statements", {"kind": "income", "ticker": "NVDA"})
    assert "SEC XBRL" in out and "亿 USD" in out and "| 收入 | 1,300.00 |" in out


def test_shareholders_tool_a_and_hk(app, monkeypatch):
    top = [{"date": "2026-06-30", "rank": 1, "name": "控股公司", "shares": 5e7, "ratio": 30.0, "change": "不变", "type": "", "share_type": ""},
           {"date": "2026-06-30", "rank": 2, "name": "某基金", "shares": 1e7, "ratio": 6.0, "change": "2500000", "type": "", "share_type": ""},
           {"date": "2026-03-31", "rank": 1, "name": "控股公司", "shares": 5e7, "ratio": 30.0, "change": "不变", "type": "", "share_type": ""},
           {"date": "2026-03-31", "rank": 2, "name": "离场者", "shares": 9e6, "ratio": 5.0, "change": "不变", "type": "", "share_type": ""}]
    monkeypatch.setattr(emdata, "a_holders", lambda session, code, free=False: top)
    monkeypatch.setattr(emdata, "a_holder_counts", lambda session, code: [
        {"date": "2026-06-30", "holders": 12000, "change_pct": -8.5, "avg_shares": 9000, "focus": "", "top10_ratio": 50}])
    monkeypatch.setattr(emdata, "hk_holders", lambda session, code: [
        {"date": "2026-06-30", "notice": "2026-08-20", "name": "创始人", "shares": 2e8, "ratio": 25.0, "role": "董事",
         "identity": "实益拥有人", "share_type": "H股"}])
    out = tools.ToolBox(app, app.watchlist.get("acme")).call("shareholders", {})
    assert "| 2 | 某基金 | 1,000 | 6.00 | 新进 |" in out and "退出十大股东：离场者" in out
    assert "与十大股东重合 2 名" in out and "12,000" in out
    assert "01234.HK 主要股东与董事权益" in out and "| 创始人 | 董事 |" in out


def test_ccass_tool_compares_with_stored_snapshots(app):
    day = today_local() - timedelta(days=1)
    for back, holdings in ((0, {"C1": 400_000, "A00003": 100_000}), (7, {"C1": 390_000, "A00003": 80_000}),
                           (28, {"C1": 300_000, "A00003": 50_000, "B5": 60_000})):
        app.db.save_ccass("01234", _snap((day - timedelta(days=back)).isoformat(), holdings), keep=60)
    out = tools.ToolBox(app, app.watchlist.get("acme")).call("ccass", {})
    assert "CCASS 持股" in out and "前 5 名参与者合计 50.00%" in out
    assert "| 参与者C1（C1） | 40 | 40.00 | +1.00 个百分点 | +10.00 个百分点 |" in out
    assert "港股通（沪）（A00003）" in out
    assert "参与者B5 -6.00 个百分点" in out  # 已跌出前 15 的大额变化另列
    assert "只适用于港股" in tools.ToolBox(app, app.watchlist.get("acme")).call("ccass", {"ticker": "600519"})


def test_broker_research_us_from_nasdaq(app, monkeypatch):
    payloads = {
        "targetprice": {"consensusOverview": {"lowPriceTarget": 11.5, "highPriceTarget": 28.0, "priceTarget": 17.88, "buy": 3, "sell": 1, "hold": 3}},
        "earnings-forecast": {"yearlyForecast": {"rows": [{"fiscalEnd": "Dec 2026", "consensusEPSForecast": -0.67, "highEPSForecast": -0.57,
                                                           "lowEPSForecast": -0.72, "noOfEstimates": 5, "up": 0, "down": 1}]}},
        "ratings": {"meanRatingType": "Buy", "brokerNames": ["A", "B"]},
    }
    monkeypatch.setattr(nasdaq, "_get", lambda session, ticker, path: payloads[path])
    c = nasdaq.consensus(None, "XPEV")
    assert c["target"]["avg"] == 17.88 and c["ratings"] == {"buy": 3, "hold": 3, "sell": 1} and c["eps"][0]["n"] == 5
    monkeypatch.setattr(tools, "latest_quote", lambda app, secid: {"price": 10.0})
    monkeypatch.setattr(app, "secid", lambda listing: "106.XPEV")
    out = tools.ToolBox(app, None).call("broker_research", {"ticker": "XPEV"})
    assert "买入 3 / 持有 3 / 卖出 1" in out and "综合评级 买入" in out and "均值 +78.8%" in out
    assert "| Dec 2026 | -0.67 | -0.57 | -0.72 | 5 | 0/1 |" in out
