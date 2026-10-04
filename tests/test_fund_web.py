"""模拟基金页面：空库与有数据时都能渲染且不联网；回执与恢复的写操作要过 CSRF / Origin；回执不改动模拟账本。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient
from test_fund import add_day_bar, funded  # noqa: F401 - pytest fixture

from stockagent.db import DB
from stockagent.fund import engine, store
from stockagent.web.server import create_app

LABEL = "模拟组合信号，非投资建议"


def client_for(project, monkeypatch) -> TestClient:
    web = create_app(project / "config.yaml", allowed_hosts=["testserver"], start_background=False)

    def no_network(*args, **kwargs):  # 页面只读库：任何联网请求都算错误
        raise AssertionError(f"页面渲染时不应联网：{args[1:3]}")

    monkeypatch.setattr(requests.Session, "request", no_network)
    c = TestClient(web)
    c.holder = web.state.holder
    c.get("/")
    return c


def post(c, url, data=None, **kw):
    return c.post(url, data={**(data or {}), "csrf_token": c.cookies.get("stock_csrf")}, follow_redirects=False, **kw)


def test_fund_pages_render_with_empty_db(project, monkeypatch):
    c = client_for(project, monkeypatch)
    for url in ("/funds", "/funds/suggestions", "/funds/actual"):
        r = c.get(url)
        assert r.status_code == 200, (url, r.text[:300])
        assert LABEL in r.text and 'href="/funds"' in r.text
    assert "funds.yaml" in c.get("/funds").text                    # 没有 funds.yaml 时提示
    assert c.get("/funds/nope").status_code == 404


@pytest.fixture
def traded(funded, app):  # noqa: F811 - 复用 test_fund 的合成股票池
    cfg, as_of = funded
    engine.plan(app, cfg, as_of)
    for s in store.suggestions(app.db, day=as_of.isoformat(), statuses=("pending",)):
        if s["kind"] == "open":
            px = s["limit_price"] * 0.99
            add_day_bar(app, s["secid"], as_of, px, px * 1.01, px * 0.98, px)
    add_day_bar(app, "1.000300", as_of, 3000, 3010, 2990, 3005)
    engine.close(app, cfg, as_of, fetch=False)
    engine.plan(app, cfg, as_of)          # 再出一次计划：有待执行的建议
    return cfg, as_of


def test_fund_pages_render_with_data_without_network(traded, project, monkeypatch, app):
    c = client_for(project, monkeypatch)
    held = store.positions(app.db, "a_trend")
    assert held
    for url in ("/funds", "/funds/a_trend", "/funds/hk_value", "/funds/suggestions?days=30", "/funds/suggestions?fund=a_trend",
                "/funds/actual"):
        r = c.get(url)
        assert r.status_code == 200, (url, r.text[:500])
        assert LABEL in r.text
    page = c.get("/funds/a_trend").text
    assert held[0]["code"] in page and "模拟持仓" in page and "单位净值" in page
    assert "已执行" in c.get("/funds/suggestions?days=30").text


def test_receipts_need_csrf_and_do_not_touch_the_model_ledger(traded, project, monkeypatch, app):
    c = client_for(project, monkeypatch)
    s = next(x for x in store.suggestions(app.db, statuses=("pending",)) if x["action"] in ("buy", "add", "sell", "trim", "stop"))
    before = (store.positions(app.db, s["fund_id"]), store.fund(app.db, s["fund_id"])["cash"], store.trades(app.db, s["fund_id"]))
    url = f"/funds/suggestions/{s['id']}/execute"
    assert c.post(url, data={"shares": "100"}, follow_redirects=False).status_code == 403            # 没有 CSRF
    assert post(c, url, {"shares": "100"}, headers={"origin": "http://evil.example"}).status_code == 403
    r = post(c, url, {"shares": "abc"})
    assert r.status_code == 303 and "err=" in r.headers["location"]
    r = post(c, url, {"shares": "200", "price": "10.5"})
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    a = store.actions(app.db, s["fund_id"])[0]
    assert a["status"] == "executed" and a["shares"] == 200 and a["price"] == 10.5 and not a["estimated"]
    after = (store.positions(app.db, s["fund_id"]), store.fund(app.db, s["fund_id"])["cash"], store.trades(app.db, s["fund_id"]))
    assert after == before                                                                          # 模拟账本不变
    assert store.suggestion(app.db, s["id"])["status"] == "pending"                                 # 建议状态不变，盘后照常结算
    if s["action"] in ("buy", "add"):
        assert store.actual_holdings(app.db, s["fund_id"])[s["secid"]]["shares"] == 200
        assert "200" in c.get("/funds/actual").text
    # 不填数量和价格：按建议数量与参考价，标为推算
    post(c, url, {})
    a = store.actions(app.db, s["fund_id"])[0]
    assert a["estimated"] and a["shares"] == s["shares"]
    r = post(c, f"/funds/suggestions/{s['id']}/ignore")
    assert r.status_code == 303 and store.actions(app.db, s["fund_id"])[0]["status"] == "ignored"
    post(c, f"/funds/suggestions/{s['id']}/clear")
    assert store.actions(app.db, s["fund_id"]) == []
    assert post(c, "/funds/suggestions/999999/execute").status_code == 404


def test_actual_holdings_average_cost():
    db = DB(Path(tempfile.mkdtemp()) / "t.db")
    store.ensure(db)
    base = {"fund_id": "f", "secid": "0.000001", "code": "000001", "name": "甲"}
    for i, (act, n, px) in enumerate([("buy", 1000, 10.0), ("add", 1000, 12.0), ("trim", 500, 13.0)], 1):
        store.record_action(db, {**base, "id": i, "action": act}, "executed", shares=n, price=px)
    h = store.actual_holdings(db, "f")["0.000001"]
    assert h["shares"] == 1500 and h["avg_cost"] == pytest.approx(11.0) and h["realized"] == pytest.approx(500 * 2.0)
    store.record_action(db, {**base, "id": 4, "action": "sell"}, "ignored")
    assert store.actual_holdings(db, "f")["0.000001"]["shares"] == 1500
    db.close()


def test_resume_needs_confirm_and_csrf(traded, project, monkeypatch, app):
    store.set_fund(app.db, "a_trend", breaker=3, halted_at="2026-10-01T00:00:00+00:00")
    c = client_for(project, monkeypatch)
    assert "恢复基金" in c.get("/funds/a_trend").text
    assert c.post("/funds/a_trend/resume", data={"confirm": "yes"}, follow_redirects=False).status_code == 403
    r = post(c, "/funds/a_trend/resume")
    assert r.status_code == 303 and "err=" in r.headers["location"] and store.fund(app.db, "a_trend")["breaker"] == 3
    r = post(c, "/funds/a_trend/resume", {"confirm": "yes"})
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    f = store.fund(app.db, "a_trend")
    assert f["breaker"] == 0 and f["halted_at"] is None
    r = post(c, "/funds/a_trend/resume", {"confirm": "yes"})
    assert "err=" in r.headers["location"]                                                          # 没有熔断时不需要恢复


FORM = {"id": "hk_div", "name": "港股高股息", "strategy": "hk_quality_value", "initial_capital": "500000", "max_holdings": "12",
        "max_weight": "12", "sector_cap": "30", "min_weight": "3", "dd1": "5", "dd2": "10", "dd3": "15", "rebalance": "monthly",
        "benchmark": "100.HSI", "status": "paper", "params": "min_roe: 10\ncheap_quantile: 0.3\n"}


def test_create_edit_and_delete_fund_through_the_form(funded, project, monkeypatch, app):  # noqa: F811
    path = project / "funds.yaml"
    path.write_text("# 我的基金配置（注释要保留）\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    c = client_for(project, monkeypatch)
    for url in ("/funds/new", "/funds/a_trend/edit"):
        r = c.get(url)
        assert r.status_code == 200 and LABEL in r.text, url
    assert c.post("/funds/save", data={**FORM, "original_id": ""}, follow_redirects=False).status_code == 403   # 没有 CSRF
    r = post(c, "/funds/save", {**FORM, "original_id": ""})
    assert r.status_code == 303 and "/funds/hk_div" in r.headers["location"], r.text[:300]
    text = path.read_text(encoding="utf-8")
    assert "# 我的基金配置（注释要保留）" in text and "hk_div" in text and 'benchmark: "100.HSI"' in text
    f = store.fund(app.db, "hk_div")
    assert f["initial_capital"] == 500000 and f["max_weight"] == 0.12 and f["params"] == {"min_roe": 10, "cheap_quantile": 0.3}
    assert any(b.name.startswith("funds.yaml.") for b in (app.data_dir / "backups").iterdir())
    # 无效输入：策略参数不认识、市场与策略不符时不写入
    before = path.read_text(encoding="utf-8")
    r = post(c, "/funds/save", {**FORM, "id": "bad_one", "original_id": "", "params": "nope: 1"})
    assert r.status_code == 400 and "nope" in r.text and path.read_text(encoding="utf-8") == before
    # 编辑：没有历史的基金可以改初始资金；调低标的上限、改为暂停
    r = post(c, "/funds/save", {**FORM, "original_id": "hk_div", "initial_capital": "800000", "status": "paused"})
    assert r.status_code == 303 and store.fund(app.db, "hk_div")["status"] == "paused"
    assert store.fund(app.db, "hk_div")["initial_capital"] == 800000
    # 有历史的基金：初始资金、策略不能改（表单提交了也忽略）
    store.add_flow(app.db, store.fund(app.db, "a_trend"), 100_000, 1.0, "2026-10-05")
    r = post(c, "/funds/save", {**FORM, "id": "a_trend", "original_id": "a_trend", "strategy": "a_sector_trend", "name": "A 趋势",
                                "initial_capital": "9999999", "params": "rs_top: 0.25\n", "benchmark": "1.000300"})
    assert r.status_code == 303, r.text[:300]
    f = store.fund(app.db, "a_trend")
    assert f["initial_capital"] == 1e6 and f["name"] == "A 趋势" and f["params"] == {"rs_top": 0.25}
    assert "初始资金不能改" in requests.utils.unquote(r.headers["location"])
    # 资金进出：CSRF、超过现金被拒、成功后份额增加
    assert c.post("/funds/a_trend/flow", data={"amount": "1000"}, follow_redirects=False).status_code == 403
    r = post(c, "/funds/a_trend/flow", {"direction": "out", "amount": "99999999"})
    assert "err=" in r.headers["location"]
    r = post(c, "/funds/a_trend/flow", {"direction": "in", "amount": "50000", "note": "加仓资金"})
    assert "ok=" in r.headers["location"] and len(store.flows(app.db, "a_trend")) == 2
    assert "资金进出" in c.get("/funds/a_trend").text
    # 删除：有历史的不行；没有历史的从 funds.yaml 与库里删除
    r = post(c, "/funds/a_trend/delete", {"confirm": "yes"})
    assert "err=" in r.headers["location"] and store.fund(app.db, "a_trend") is not None
    r = post(c, "/funds/hk_div/delete")
    assert "err=" in r.headers["location"]                                            # 没勾确认
    r = post(c, "/funds/hk_div/delete", {"confirm": "yes"})
    assert r.headers["location"].startswith("/funds?ok=") and store.fund(app.db, "hk_div") is None
    assert "hk_div" not in path.read_text(encoding="utf-8") and "# 我的基金配置（注释要保留）" in path.read_text(encoding="utf-8")


def test_superseded_suggestions_are_hidden_by_default(traded, project, monkeypatch, app):
    cfg, as_of = traded
    engine.plan(app, cfg, as_of)                    # 同一天再出一次计划：上一版建议被取代
    old = [s for s in store.suggestions(app.db) if s.get("note") == "被新计划取代"]
    assert old
    c = client_for(project, monkeypatch)
    page = c.get("/funds/suggestions?days=30").text
    assert f"已隐藏被新计划取代的 {len(old)} 条旧建议" in page
    page = c.get("/funds/suggestions?days=30&old=1").text
    assert "正在显示被新计划取代的" in page and f'/funds/suggestions/{old[0]["id"]}/execute' not in page
