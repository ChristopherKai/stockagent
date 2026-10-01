"""业绩 vs 一致预期：研报按时点还原一致预期、年报偏差与中报完成度外推、检查新业绩生成数据条目（只生成一次）、
港股手动录入的快照、分级时不被并入公告、工具与 WebUI 录入。全部离线（替换东方财富接口）。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from test_tracker import _bars
from test_web import post, web  # noqa: F401 - web 是 fixture

from stockagent import consensus, triage
from stockagent.config import Company, Listing
from stockagent.llm import LLMResult
from stockagent.sources import emdata
from stockagent.tools import ToolBox
from stockagent.utils import today_local

TODAY = today_local()
FY = TODAY.year


def a_rows(notice: str) -> list[dict]:
    """emdata.financials 形状的 A 股主要指标（报告期倒序，年初至今累计）：本期中报 + 近 3 年的中报与年报。"""
    def row(period: str, eps: float, ann: str) -> dict:
        return {"期间": period, "公告日": ann, "EPS": eps, "归母净利": eps * 11.0e8, "营业收入": eps * 40e8, "币种": "CNY",
                "_report_date": period}
    def ago(days: int) -> str:  # 旧报告期的公告日都在首次检查的回看窗口之外
        return (TODAY - timedelta(days=days)).isoformat()
    return [row(f"{FY}-06-30", 12.31, notice),
            row(f"{FY - 1}-12-31", 9.80, ago(200)), row(f"{FY - 1}-06-30", 3.64, ago(400)),
            row(f"{FY - 2}-12-31", 4.72, ago(560)), row(f"{FY - 2}-06-30", 2.16, ago(760)),
            row(f"{FY - 3}-12-31", 2.00, ago(920)), row(f"{FY - 3}-06-30", 0.77, ago(1120))]


def reports(notice: date) -> list[dict]:
    """research_reports 形状：同一机构两篇取最新；发布日在业绩之后的不算。"""
    def r(days_before: int, org: str, eps: float) -> dict:
        return {"date": (notice - timedelta(days=days_before)).isoformat(), "org": org, "years": {FY: eps, FY + 1: eps * 1.8}}
    return [r(5, "甲证券", 30.0), r(60, "甲证券", 25.0), r(20, "乙证券", 31.0), r(40, "丙证券", 29.0), r(-3, "丁证券", 40.0),
            r(400, "戊证券", 10.0)]


def test_report_years_maps_by_publish_year():
    x = {"publishDate": "2026-04-23 00:00:00.000", "predictThisYearEps": "26.87", "predictNextYearEps": "51.98",
         "predictNextTwoYearEps": "74.99", "predictLastYearEps": ""}
    assert emdata.report_years(x) == {2026: 26.87, 2027: 51.98, 2028: 74.99}
    assert emdata.report_years({**x, "predictLastYearEps": "9.5"})[2025] == 9.5


def test_point_in_time_consensus_takes_latest_per_org():
    notice = date(FY, 8, 22)
    c = consensus.a_consensus_at(reports(notice), FY, notice, 120)
    assert c["n"] == 3 and c["value"] == 30.0 and (c["low"], c["high"]) == (29.0, 31.0)  # 甲取新的一篇；丁在发布之后；戊在窗口之外
    assert c["as_of"] == (notice - timedelta(days=5)).isoformat()
    assert consensus.a_consensus_at(reports(notice), FY + 5, notice, 120) is None


def test_compare_annual_interim_loss_and_forecast():
    history = consensus.actual_rows(a_rows(f"{FY}-08-22"), "A")
    cons = {"fy": FY - 1, "metric": "eps", "value": 9.72, "n": 6, "low": 9.0, "high": 10.5, "as_of": f"{FY}-03-20",
            "currency": "CNY", "source": "测试", "flags": []}
    annual = consensus.compare(history[1], cons, history, "report")
    assert annual["kind"] == "annual" and annual["surprise"] == pytest.approx(9.80 / 9.72 - 1)
    assert "+0.8%" in annual["headline"] and "较发布前一致预期" in annual["headline"]

    cons = {**cons, "fy": FY, "value": 30.27, "as_of": f"{FY}-07-20"}
    interim = consensus.compare(history[0], cons, history, "report")
    norm = (3.64 / 9.80 + 2.16 / 4.72 + 0.77 / 2.00) / 3
    assert interim["completion"] == pytest.approx(12.31 / 30.27) and interim["norm"]["mean"] == pytest.approx(norm)
    assert interim["implied_fy"] == pytest.approx(12.31 / norm) and interim["implied_gap"] == pytest.approx(12.31 / norm / 30.27 - 1)
    assert "完成全年一致预期" in interim["headline"] and "近 3 年同期平均 40%" in interim["headline"]

    loss = consensus.compare({**history[0], "eps": -1.63}, {**cons, "value": -2.4}, history, "report")
    assert loss["rest"] == pytest.approx(-2.4 + 1.63) and "completion" not in loss
    assert any("亏损" in f for f in loss["flags"]) and "余下期间" in loss["headline"]

    few = consensus.compare(history[0], {**cons, "n": 1, "as_of": f"{FY - 1}-01-01"}, history, "report")
    assert any("样本少" in f for f in few["flags"]) and any("可能过时" in f for f in few["flags"])

    fc = {"period_end": f"{FY}-09-30", "notice": f"{FY}-10-10", "currency": "CNY", "eps": 9.9, "eps_low": 9.0, "eps_high": 10.8}
    res = consensus.compare(fc, cons, history, "forecast")
    assert "业绩预告" in res["headline"] and "区间" in res["headline"]
    title, snippet = consensus.item_text(res)
    assert title.startswith("业绩对比一致预期｜") and snippet.startswith(consensus.TAG)


def _stub_a(monkeypatch, notice: date, forecasts=()):
    monkeypatch.setattr(emdata, "financials", lambda session, listing, periods=8: a_rows(notice.isoformat()))
    monkeypatch.setattr(emdata, "research_reports", lambda *a, **k: reports(notice))
    monkeypatch.setattr(emdata, "a_forecasts", lambda *a, **k: list(forecasts))
    monkeypatch.setattr(emdata, "a_consensus", lambda *a, **k: {"years": [{"fy": FY, "actual": False, "eps": 28.8}], "orgs": 20,
                                                                "buy": 18, "add": 2, "target_high": None, "target_low": None})


def test_check_creates_one_item_per_period(app, monkeypatch):
    notice = TODAY - timedelta(days=2)
    fc = {"period_end": f"{FY}-09-30", "notice": (TODAY - timedelta(days=1)).isoformat(), "low": 100e8, "high": 120e8,
          "type": "预增", "prev": None, "content": "", "finance": "归属于上市公司股东的净利润"}
    _stub_a(monkeypatch, notice, [fc])
    acme = app.watchlist.get("acme")
    msgs = consensus.snapshot(app, [acme]) + consensus.check(app, [acme])
    assert any("完成全年一致预期" in m for m in msgs) and any("业绩预告" in m for m in msgs)
    items = [i for i in app.db.items_since("acme", "0000") if i["source"] == "consensus"]
    assert len(items) == 2 and all(i["kind"] == "data" and i["listing"] == "HK:01234" for i in items)  # 价格反应按定价上市地（H 股）
    assert all(i["snippet"].startswith(consensus.TAG) for i in items)
    rows = app.db.earnings("acme")
    assert {r["stage"] for r in rows} == {"report", "forecast"} and all(r["item_uid"] for r in rows)
    report = next(r for r in rows if r["stage"] == "report")["data"]
    assert report["consensus"]["n"] == 3 and report["consensus"]["value"] == 30.0
    assert any("A+H" in f for f in report["flags"])
    forecast = next(r for r in rows if r["stage"] == "forecast")["data"]
    assert forecast["actual"] == pytest.approx(110e8 / (12.31 * 11.0e8 / 12.31))  # 归母净利中点 ÷ 股本
    # 再跑一次不重复；更早的报告期在首次检查的回看窗口之外，不补算
    assert consensus.check(app, [acme]) == []
    assert len([i for i in app.db.items_since("acme", "0000") if i["source"] == "consensus"]) == 2
    assert len(app.db.earnings("acme")) == 2
    snap = app.db.consensus_latest("acme")
    assert snap and snap[0]["source"] == "em_a" and snap[0]["value"] == 28.8


def hk_rows(periods: list[tuple[str, float]]) -> list[dict]:
    return [{"期间": p, "EPS": e, "归母净利": e * 10e8, "营业额": e * 50e8, "币种": "CNY", "_report_date": p} for p, e in periods]


def test_hk_manual_consensus_flow(app, monkeypatch):
    _bars(app, "133.HKDCNH", [0.9] * 10)
    co = Company(id="hkco", name="港股公司", listings=[Listing(market="HK", code="00100")])
    hist = [(f"{FY - 1}-12-31", 2.0), (f"{FY - 1}-06-30", 0.9), (f"{FY - 2}-12-31", 1.8), (f"{FY - 2}-06-30", 0.8)]
    monkeypatch.setattr(emdata, "financials", lambda s, l, periods=8: hk_rows([(f"{FY}-06-30", 1.1)] + hist))
    cache = consensus.ReportCache(app)
    assert consensus.check_company(app, co, cache) == []  # 首次检查：最新一期不是近期发布的，只记水位
    # 新报告期出现，但还没有一致预期：提示一次
    monkeypatch.setattr(emdata, "financials", lambda s, l, periods=8: hk_rows([(f"{FY}-12-31", 2.6), (f"{FY}-06-30", 1.1)] + hist))
    msgs = consensus.check_company(app, co, cache)
    assert len(msgs) == 1 and "没有发布前的一致预期快照" in msgs[0]
    assert consensus.check_company(app, co, cache) == []  # 不重复提醒
    # 补录发布日之前的一致预期（港元），换成报告币种（人民币）对比
    msg = consensus.save_manual(app, co, FY, "eps", 2.5, "HKD", (TODAY - timedelta(days=1)).isoformat(), 8, "富途")
    assert "已保存" in msg and "2.50 港元" in msg
    msgs = consensus.check_company(app, co, cache)
    assert len(msgs) == 1 and "较发布前一致预期" in msgs[0]
    res = app.db.earnings("hkco")[0]["data"]
    assert res["consensus"]["value"] == pytest.approx(2.5 * 0.9) and res["surprise"] == pytest.approx(2.6 / 2.25 - 1)
    assert any("汇率" in f for f in res["flags"]) and res["consensus"]["n"] == 8
    assert consensus.check_company(app, co, cache) == []
    with pytest.raises(ValueError, match="指标"):
        consensus.save_manual(app, co, FY, "pe", 1, "HKD")
    with pytest.raises(ValueError, match="币种"):
        consensus.save_manual(app, co, FY, "eps", 1, "EUR")
    with pytest.raises(ValueError, match="晚于今天"):
        consensus.save_manual(app, co, FY, "eps", 1, "HKD", (TODAY + timedelta(days=3)).isoformat())


def test_consensus_items_are_never_marked_duplicate(app, monkeypatch):
    notice = TODAY - timedelta(days=1)
    _stub_a(monkeypatch, notice)
    acme = app.watchlist.get("acme")
    consensus.check(app, [acme])
    data = next(i for i in app.db.untriaged("acme") if i["source"] == "consensus")
    other = triage.short_id("x" * 12)

    def complete(task, system, user, *a, **k):
        assert "程序计算" in user and "一致预期对比" in user
        return LLMResult(text="", data={"items": [{
            "id": triage.short_id(data["uid"]), "relevant": False, "duplicate_of": other, "event_type": "earnings",
            "direction": "positive", "materiality": 4, "novelty": "new", "horizon": "weeks", "summary_zh": "中报好于预期",
            "expectation": "外推全年高于一致预期", "follow_up": ""}]}, model="fake", profile="mock", usage={})

    monkeypatch.setattr(app.llm, "complete", complete)
    triage.triage_company(app, acme)
    row = next(i for i in app.db.items_since("acme", "0000") if i["uid"] == data["uid"])
    assert row["duplicate_of"] == "" and row["relevant"] == 1 and row["materiality"] == 4


def test_tool_and_report_text(app, monkeypatch):
    _stub_a(monkeypatch, TODAY - timedelta(days=2))
    out = ToolBox(app, app.watchlist.get("acme")).call("earnings_vs_consensus", {})
    assert "当前一致预期" in out and "历史各期" in out and "完成全年一致预期" in out and "研报中位数" in out
    out = ToolBox(app, None).call("earnings_vs_consensus", {"ticker": "600519"})
    assert "历史各期" in out
    assert "不在自选股里" in ToolBox(app, None).call("earnings_vs_consensus", {"ticker": "00700"})


def test_web_consensus_card_and_entry(web):  # noqa: F811 - web 是 fixture
    r = web.get("/c/dongyue")
    assert r.status_code == 200 and "业绩 vs 一致预期" in r.text and "录入一致预期" in r.text
    r = post(web, "/c/dongyue/consensus", {"year": str(FY), "metric": "eps", "value": "1.35", "currency": "CNY", "n": "9",
                                            "date": (TODAY - timedelta(days=2)).isoformat(), "source": "富途"})
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    rows = web.holder.app.db.consensus_rows("dongyue")
    assert len(rows) == 1 and rows[0]["source"] == "manual" and rows[0]["value"] == 1.35 and rows[0]["n"] == 9
    page = web.get("/c/dongyue").text
    assert "使用者录入·富途" in page and "1.35 元" in page
    r = post(web, "/c/dongyue/consensus", {"year": str(FY), "metric": "eps", "value": "abc", "currency": "CNY"})
    assert "err=" in r.headers["location"]
    r = post(web, f"/c/dongyue/consensus/{rows[0]['id']}/delete")
    assert "ok=" in r.headers["location"] and web.holder.app.db.consensus_rows("dongyue") == []
    assert "err=" in post(web, "/c/dongyue/consensus/999/delete").headers["location"]


def test_hk_results_announcement_vs_full_interim_report(app, monkeypatch):
    """港股的业绩以 [中期業績] 类别公告发布；一个月后刊发的「中期報告」全文不算业绩发布（首次检查时不应当成新业绩）。"""
    from stockagent.app import _item
    from stockagent.utils import utcnow

    monkeypatch.setattr(emdata, "financials", lambda s, l, periods=8: hk_rows([(f"{FY}-06-30", 1.1), (f"{FY - 1}-12-31", 2.0)]))
    report_doc = Company(id="hkdoc", name="甲", listings=[Listing(market="HK", code="00100")])
    results = Company(id="hkres", name="乙", listings=[Listing(market="HK", code="02367")])
    for co, cat, title in ((report_doc, "財務報表/環境、社會及管治資料 - [中期/半年度報告]", "2026年中期報告"),
                           (results, "公告及通告 - [中期業績]", "截至2026年6月30日止六個月之中期業績公告")):
        x = {"source_id": title, "title": title, "published_at": utcnow() - timedelta(days=2), "url": "https://www1.hkexnews.hk/x.pdf"}
        app.db.add_item(_item(co, co.listings[0], "announcement", "hkex", x, cat))
    cache = consensus.ReportCache(app)
    assert consensus.check_company(app, report_doc, cache) == []
    msgs = consensus.check_company(app, results, cache)
    assert len(msgs) == 1 and "没有发布前的一致预期快照" in msgs[0]


def test_thin_report_library_falls_back_to_aggregate_snapshot(app, monkeypatch):
    """研报库只收录 1 篇时，改用发布前的机构预测汇总快照（机构更多）。"""
    notice = TODAY - timedelta(days=2)
    monkeypatch.setattr(emdata, "research_reports", lambda *a, **k: [
        {"date": (notice - timedelta(days=9)).isoformat(), "org": "甲证券", "years": {FY: 6.05}}])
    acme = app.watchlist.get("acme")
    app.db.save_consensus("acme", "em_a", (notice - timedelta(days=5)).isoformat(),
                          [{"fiscal_year": FY, "metric": "eps", "value": 8.87, "currency": "CNY", "n": 11}], listing="A:300001")
    c = consensus.consensus_for(app, acme, FY, notice, "CNY", consensus.ReportCache(app))
    assert c["value"] == 8.87 and c["n"] == 11 and "汇总快照" in c["source"]
    app.db.save_consensus("acme", "em_a", notice.isoformat(), [{"fiscal_year": FY, "metric": "eps", "value": 1, "n": 30}])
    assert consensus.consensus_for(app, acme, FY, notice, "CNY", consensus.ReportCache(app))["value"] == 8.87  # 发布当天的快照不算
