"""WebUI 改版：各上市地独立视图、观察清单排序 / 置顶 / 筛选与追踪开关、报告历史与删除、报告目录、问答模型路由。全部离线。"""

from __future__ import annotations

import pytest
from test_multilisting import mapp  # noqa: F401 - fixture
from test_tracker import CARD, REPORT, _bars, save_dossier
from test_web import post, web  # noqa: F401 - fixture

from stockagent import valuation as V
from stockagent.cli import LLM_TASKS
from stockagent.web import settings_views, yamledit
from stockagent.web.views import watch_rows


def _model(listing="A:300308", ccy="CNY"):
    return V.ValuationModel(driver_label="价", multiple_label="—", shares=1, price_currency=ccy, report_currency=ccy, listing=listing,
                            scenarios=[V.ScenarioPoint(name="base", driver=900, multiple=1, probability=1)])


def test_listing_views_split_by_listing(mapp):  # noqa: F811
    """A 股折价、H 股溢价：同一份估值在两地有不同的目标价、空间和立场。"""
    _bars(mapp, "0.300308", [700.0] * 30)
    _bars(mapp, "116.03308", [1300.0] * 30)
    c = mapp.watchlist.get("innolight")
    a, h = V.listing_views(mapp, c, _model(), 0.15)  # listings 顺序：A、HK
    assert (a["key"], a["primary"], a["stance"]) == ("A:300308", True, "bullish")
    assert a["target"] == pytest.approx(900) and a["upside"] == pytest.approx(900 / 700 - 1)
    assert (h["key"], h["primary"], h["stance"]) == ("HK:03308", False, "bearish")
    assert h["target"] == pytest.approx(900 / 0.9)  # 汇率平价：1 港元 = 0.9 人民币
    assert h["upside"] == pytest.approx(1000 / 1300 - 1) and h["premium"] == pytest.approx(1300 * 0.9 / 700 - 1)
    assert h["scenarios"]["base"]["price"] == pytest.approx(1000)
    assert V.view_sort_key([a, h])[0] == 0  # 取最靠前的观点（偏多）


def test_listing_views_without_conversion_data(mapp):  # noqa: F811
    _bars(mapp, "116.09868", [39.0] * 30)
    _bars(mapp, "106.XPEV", [11.0] * 30)
    c = mapp.watchlist.get("xpeng")
    mapp.watchlist.get("xpeng").listings[1].ads_ratio = None
    hk, us = V.listing_views(mapp, c, _model("HK:09868", "HKD"), 0.15)
    assert hk["stance"] and us["price"] == 11.0 and not us["convertible"] and us["target"] is None and us["stance"] is None
    assert V.view_sort_key([us]) == (9, 0, 0)


def test_watchlist_sorted_filterable_and_pinnable(web):  # noqa: F811
    html = web.get("/companies").text
    for hint in ("data-filter-bar", 'data-f="market"', 'data-f="stance"', 'data-f="tracked"', "data-group", 'data-market="HK"'):
        assert hint in html
    for gone in ("<th>行业</th>", "<th>推特账号</th>", "<th>上市地</th><th>行业</th>", "7 日事件", "<th class=\"r\">待补充</th>", "<th>画像</th>", "<th>深度报告</th>"):
        assert gone not in html
    for needed in ("目标价 / 空间", "牛 / 基 / 熊", "多空比", "报告日期", "报告立场", "情景立场", "重跑报告", 'role="switch"', 'data-f="sort"'):
        assert needed in html
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
