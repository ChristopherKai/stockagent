"""WebUI 改版：各上市地独立视图、观察清单排序 / 置顶 / 筛选与追踪开关、报告历史与删除、报告目录、问答模型路由。全部离线。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from test_multilisting import mapp  # noqa: F401 - fixture
from test_tracker import CARD, REPORT, _bars, save_dossier
from test_web import post, web  # noqa: F401 - fixture

from stockagent import valuation as V
from stockagent.cli import LLM_TASKS
from stockagent.utils import iso, utcnow
from stockagent.web import settings_views, yamledit
from stockagent.web.views import watch_rows


def _model(listing="A:300308", ccy="CNY"):
    return V.ValuationModel(driver_label="价", multiple_label="—", shares=1, price_currency=ccy, report_currency=ccy, listing=listing,
                            scenarios=[V.ScenarioPoint(name="base", driver=900, multiple=1, probability=1)])


def _bar_day(app, secid, i):
    return str(app.bars(secid, limit=100)["date"].iloc[i])


def test_listing_views_convert_by_price_ratio(mapp):  # noqa: F811
    """其他上市地按两地现价之比折算（与报告的等值价格同一口径）：各地情景价、空间、立场与定价地一致；汇率平价只给出溢价。"""
    _bars(mapp, "0.300308", [700.0] * 30)
    _bars(mapp, "116.03308", [1300.0] * 30)
    c = mapp.watchlist.get("innolight")
    a, h = V.listing_views(mapp, c, _model(), 0.15)  # listings 顺序：A、HK
    assert (a["key"], a["primary"], a["stance"]) == ("A:300308", True, "bullish")
    assert a["target"] == pytest.approx(900) and a["upside"] == pytest.approx(900 / 700 - 1)
    assert (h["key"], h["primary"], h["stance"]) == ("HK:03308", False, "bullish")
    assert h["target"] == pytest.approx(900 * 1300 / 700)           # 900 元 × 两地现价之比（不是汇率平价的 1000 港元）
    assert h["upside"] == pytest.approx(a["upside"]) and h["scenarios"]["base"]["price"] == pytest.approx(h["target"])
    assert h["stats"]["p_below"] == pytest.approx(a["stats"]["p_below"])
    assert h["premium"] == pytest.approx(1300 * 0.9 / 700 - 1)      # 汇率平价：1 港元 = 0.9 人民币，H 股较 A 股溢价
    assert V.view_sort_key([a, h])[0] == 0  # 取最靠前的观点（偏多）
    # 给了报告日：价格之比取那天的收盘价，不随之后的两地价差浮动；缺那天的行情退回现价之比
    _bars(mapp, "0.300308", [650.0] * 10 + [700.0] * 20)
    h2 = V.listing_views(mapp, c, _model(), 0.15, ref_day=_bar_day(mapp, "0.300308", 0))[1]
    assert h2["target"] == pytest.approx(900 * 1300 / 650) and h2["upside"] == pytest.approx(h2["target"] / 1300 - 1)
    assert V.listing_views(mapp, c, _model(), 0.15, ref_day="1999-01-01")[1]["target"] == pytest.approx(900 * 1300 / 700)
    assert V.view_sort_key([a, h], "bearish")[0] == 2 and V.view_sort_key([a, h], "neutral")[0] == 1   # 页面上显示的立场优先


def test_listing_views_without_conversion_data(mapp):  # noqa: F811
    """没有定价上市地的行情就折算不了；有行情时不再需要汇率和 ads_ratio。"""
    _bars(mapp, "106.XPEV", [11.0] * 30)
    c = mapp.watchlist.get("xpeng")
    mapp.watchlist.get("xpeng").listings[1].ads_ratio = None
    hk, us = V.listing_views(mapp, c, _model("HK:09868", "HKD"), 0.15)
    assert hk["price"] is None and us["price"] == 11.0 and not us["convertible"] and us["target"] is None and us["stance"] is None
    assert V.view_sort_key([us]) == (9, 0, 0)
    _bars(mapp, "116.09868", [39.0] * 30)
    hk, us = V.listing_views(mapp, c, _model("HK:09868", "HKD"), 0.15)
    assert us["convertible"] and us["upside"] == pytest.approx(hk["upside"]) and us["premium"] is None   # 无换股比例：只缺溢价


def test_watchlist_sorted_filterable_and_pinnable(web):  # noqa: F811
    html = web.get("/companies").text
    for hint in ("data-filter-bar", 'data-f="market"', 'data-f="stance"', 'data-f="tracked"', "data-group", 'data-market="HK"'):
        assert hint in html
    for gone in ("<th>行业</th>", "<th>推特账号</th>", "<th>上市地</th><th>行业</th>", "7 日事件", "<th class=\"r\">待补充</th>", "<th>画像</th>", "<th>深度报告</th>"):
        assert gone not in html
    for needed in ("目标价 / 空间", "牛 / 基 / 熊", "多空比", "报告日期", "报告日价格 / 现价（至今）", "报告立场", "重跑报告", 'role="switch"', 'data-f="sort"'):
        assert needed in html
    for gone in ("情景立场", "<th>上市地</th>", "至今</th>"):
        assert gone not in html and gone not in web.get("/").text
    assert 'class="mkt-seg"' in html and 'data-v="HK"' in html and 'data-v="A"' in html and 'data-v="US"' in html and '<select data-f="market"' not in html
    assert '<div class="muted small code"' in html and "00189.HK" in html      # 上市地代码放在公司名下面
    assert "立即检查" not in html and ">论点<" not in html and "氟硅化工" not in html  # 行业、论点列、立即检查已去掉
    rows = watch_rows(web.holder.app)
    assert rows[0]["company"].id == "dongyue" and rows[0]["enabled"] and rows[0]["views"][0]["target"]
    assert not rows[0]["pinned"]
    r = post(web, "/companies/dongyue/pin", {"pinned": "1"})
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    assert watch_rows(web.holder.app)[0]["pinned"] and 'data-pinned="1"' in web.get("/companies").text
    post(web, "/companies/dongyue/pin", {"pinned": "0"})
    assert not watch_rows(web.holder.app)[0]["pinned"]
    assert post(web, "/companies/nope/pin", {"pinned": "1"}).status_code == 404


def test_track_toggle_and_check_return_to_list(web):  # noqa: F811
    r = post(web, "/c/dongyue/untrack", {"next": "/companies"})
    assert r.headers["location"].startswith("/companies?ok=")
    assert not watch_rows(web.holder.app)[0]["enabled"]
    r = post(web, "/c/dongyue/track", {"next": "/companies"})
    assert r.headers["location"].startswith("/companies?ok=") and watch_rows(web.holder.app)[0]["enabled"]
    r = post(web, "/c/dongyue/dossier", {"next": "/companies"})
    assert r.headers["location"].startswith("/companies?ok=") and web.holder.app.db.jobs(1)[0]["task"] == "dossier"
    r = post(web, "/c/dongyue/check", {"next": "/"})
    assert r.headers["location"].startswith("/?ok=")
    assert post(web, "/c/dongyue/untrack", {"next": "//evil.example"}).headers["location"].startswith("/c/dongyue?ok=")  # 只接受站内路径


def test_dashboard_hides_intact_thesis(web):  # noqa: F811
    a = web.holder.app
    html = web.get("/").text
    assert "论点完好" not in html and "data-filter-bar" in html and 'data-f="tracked"' not in html
    assert "立即检查" in html and "牛 / 基 / 熊" not in html and "情景加权估值</th>" not in html and "目标价 / 空间" in html
    a.db._exec("UPDATE revisions SET thesis_status='weakened'")
    assert "论点削弱" in web.get("/").text


def test_report_history_and_delete(web):  # noqa: F811
    a = web.holder.app
    first = a.db.latest_dossier("dongyue")["id"]
    import json
    a.db._exec("UPDATE dossiers SET card_json=? WHERE id=?", (json.dumps({**CARD, "catalysts": [{"when": "2026-12", "event": "配额正式稿", "kpi": ""}]}), first))
    second = save_dossier(a, {**CARD, "stance": "bearish"})  # 第二版；追踪的基线仍是第一版
    page = web.get("/c/dongyue/reports").text
    assert f"#{first}" in page and f"#{second}" in page and "追踪基线" in page and "报告日价格" in page
    assert "偏空" in page and "偏多" in page
    assert "<select" in web.get("/c/dongyue/report").text and 'data-toc-card' in web.get("/c/dongyue/report").text
    # 正在追踪的基线不能删
    r = post(web, f"/c/dongyue/reports/{first}/delete")
    assert "err=" in r.headers["location"] and a.db.dossier(first)
    # 最新一版可以删，上一版恢复为当前
    r = post(web, f"/c/dongyue/reports/{second}/delete")
    assert "ok=" in r.headers["location"] and a.db.dossier(second) is None
    assert a.db.latest_dossier("dongyue")["id"] == first
    assert [t["dossier_id"] for t in a.db.triggers("dongyue")] == [first]
    assert a.db.catalysts("dongyue") and a.db.plan("dongyue")
    # 停止追踪后可以删掉唯一的一版：追踪器随之清理，页面不崩
    post(web, "/c/dongyue/untrack")
    r = post(web, f"/c/dongyue/reports/{first}/delete")
    assert "ok=" in r.headers["location"] and a.db.latest_dossier("dongyue") is None
    assert a.db.tracker("dongyue") is None and not a.db.revisions("dongyue")
    for url in ("/c/dongyue/reports", "/c/dongyue", "/companies", "/"):
        assert web.get(url).status_code == 200, url
    assert web.get("/c/dongyue/report").status_code == 404
    assert "err=" in post(web, "/c/dongyue/reports/999/delete").headers["location"]


def test_chat_model_route_saves_and_takes_effect(web):  # noqa: F811
    assert set(settings_views.TASK_LABEL) == set(LLM_TASKS)  # 每个任务都出现在路由表单里（以前缺 chat）
    h = web.holder
    h.config_path.write_text(h.config_path.read_text(encoding="utf-8").replace(
        "    mock: {protocol: mock, model: mock}", "    mock: {protocol: mock, model: mock}\n    alt: {protocol: mock, model: alt-model}"), encoding="utf-8")
    h.reload()
    assert 'name="task_chat"' in web.get("/settings/llm").text
    _, etag = yamledit.load(h.config_path)
    r = post(web, "/settings/llm/routing", {"default_profile": "mock", "task_chat": "alt", "task_screen": "alt", "etag": etag})
    assert "ok=" in r.headers["location"]
    assert "alt-model" in h.app.llm.describe("chat") and "alt-model" in h.app.llm.describe("screen")
    assert "alt-model" in web.get("/c/dongyue/report").text  # 页面显示的对话模型随之改变
    # 表单里没有的任务不会被清掉
    _, etag = yamledit.load(h.config_path)
    post(web, "/settings/llm/routing", {"default_profile": "mock", "task_chat": "mock", "etag": etag})
    assert "alt-model" in h.app.llm.describe("screen") and "alt-model" not in h.app.llm.describe("chat")


def test_watchlist_and_dashboard_follow_revision_stance(web):  # noqa: F811
    """追踪修正改了立场（如 #222 由偏空转偏多）：观察清单、仪表盘显示、筛选、排序用的是当前立场，不是深度报告卡片里的旧立场。"""
    a = web.holder.app
    d = a.db.latest_dossier("dongyue")
    tr = a.db.tracker("dongyue")
    assert d["card"]["stance"] == "bullish"
    a.db._exec("UPDATE revisions SET stance='bearish' WHERE id=?", (tr["revision_id"],))
    row = watch_rows(a)[0]
    assert row["stance"] == "bearish" and row["report_stance"] == "bullish" and row["key"][0] == 2
    for url in ("/", "/companies"):
        html = web.get(url).text
        assert 'data-stance="bearish"' in html and 'data-stance="bullish"' not in html and "已修正" in html, url
    # 修正建立在旧的一版报告上（新报告还没有重新建基线）：回到最新报告的立场
    a.db._exec("UPDATE revisions SET dossier_id=dossier_id+999 WHERE id=?", (tr["revision_id"],))
    assert watch_rows(a)[0]["stance"] == "bullish"


def test_report_prices_per_listing(mapp):  # noqa: F811
    """多地上市：每个上市地各有报告日价格 / 现价 / 至今；定价上市地用报告的价格，其他上市地取报告日的收盘价。"""
    from stockagent.web.views import report_prices
    _bars(mapp, "0.300308", [700.0] * 25 + [770.0] * 5)
    _bars(mapp, "116.03308", [1300.0] * 25 + [1170.0] * 5)
    c = mapp.watchlist.get("innolight")
    dossier_id = save_dossier(mapp, {**CARD, "price_listing": "HK:03308", "stats": {"price": 1250.0}},
                            created_at=iso(utcnow() - timedelta(days=10)))
    views = V.listing_views(mapp, c, _model("HK:03308", "HKD"), 0.15)
    out = report_prices(mapp, c, mapp.db.dossier(dossier_id), views)
    assert out["HK:03308"]["price"] == 1250.0 and out["HK:03308"]["now"] == 1170.0 and out["HK:03308"]["since"] == pytest.approx(1170 / 1250 - 1)
    assert out["A:300308"]["price"] == 700.0 and out["A:300308"]["now"] == 770.0 and out["A:300308"]["since"] == pytest.approx(0.1)
    assert out["A:300308"]["currency"] == "CNY" and out["HK:03308"]["currency"] == "HKD"
