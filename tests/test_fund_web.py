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
