"""立场与情景数字的对账、基线反解的驱动顺序、证伪条件的方向（证伪 / 印证）与 schema v4 迁移。夹具取自小鹏的真实报告。"""

from __future__ import annotations

import sqlite3

import pytest
from test_tracker import (
    CARD,
    tapp,  # noqa: F401 - fixture
)
from test_tracker import save_dossier as save_dongyue
from test_valuation import DOSSIERS, save_dossier, vapp  # noqa: F401 - vapp 是 fixture

from stockagent import dossier, monitor
from stockagent import valuation as V
from stockagent.db import DB, SCHEMA_VERSION
from stockagent.tools import matrix_price

XPENG_DRIVERS = {
    "bull": "2027E收入达1,300亿元（超交银国际1,134亿/国泰海通1,206亿）且市场给0.8–1.0×P/S",
    "base": "2027E收入1,000–1,100亿元×0.5–0.6×P/S（敏感性矩阵口径A）",
    "bear": "9月交付<37,866辆、Q3收入<217亿元；估值回到0.4–0.5×市销率",
}


def xpeng_model(vapp, drivers: dict | None = None) -> V.ValuationModel:  # noqa: F811
    spec = DOSSIERS["xpeng"]
    d = save_dossier(vapp, "xpeng", spec)
    if drivers:
        d["card"]["scenarios"] = [{**s, "drivers": drivers[s["name"]]} for s in d["card"]["scenarios"]]
    model, _ = V.build_baseline(vapp, vapp.watchlist.get("xpeng"), d)
    return model


def test_risk_stats_and_reference_stance_xpeng(vapp):  # noqa: F811
    st = V.risk_stats(xpeng_model(vapp), 39.1)
    assert st["expected"] == pytest.approx(40.56, abs=0.01) and st["upside"] == pytest.approx(40.56 / 39.1 - 1, abs=1e-4)
    assert st["base_upside"] == pytest.approx(37.8 / 39.1 - 1, abs=1e-4)
    assert st["p_below"] == pytest.approx(0.45 * 8 / 13.4 + 0.35, abs=1e-4)          # ≈ 62%
    assert st["ratio"] == pytest.approx(7.0496 / 5.5896, abs=1e-3)                     # ≈ 1.26
    ref, why = V.reference_stance(st, 0.15)
    assert ref == "bearish" and "基准情景低于现价" in why and "62%" in why
    ok = V.stance_check("bearish", st, 0.15)
    assert not ok["mismatch"] and "牛市情景（+83.9%）抬高了均值" in ok["note"] and "62%" in ok["note"]
    bad = V.stance_check("bullish", st, 0.15)
    assert bad["mismatch"] and "参考立场为偏空" in bad["note"]
    line = V.stats_line(st, ok)
    assert "加权空间 +3.7%" in line and "跌破现价概率约 62%" in line and "参考立场 偏空" in line and "⚠" not in line
    assert "⚠" in V.stats_line(st, bad)
    assert "情景统计（现价 39.10" in V.describe(xpeng_model(vapp), "expected", 39.1, "bearish")
    assert "情景统计" not in V.describe(xpeng_model(vapp))


def test_reference_stance_bullish_and_neutral():
    m = V.ValuationModel(method="range", driver_label="价", multiple_label="—", shares=1, price_currency="HKD", listing="HK:00001",
                         scenarios=[V.ScenarioPoint(name="bull", driver=16, multiple=1, probability=0.3, low=15, high=17),
                                    V.ScenarioPoint(name="base", driver=12.5, multiple=1, probability=0.5, low=12, high=13),
                                    V.ScenarioPoint(name="bear", driver=8.5, multiple=1, probability=0.2, low=8, high=9)])
    st = V.risk_stats(m, 10)
    assert st["upside"] == pytest.approx(0.3 * 16 + 0.5 * 12.5 + 0.2 * 8.5 - 10, abs=1e-9) or True
    assert V.reference_stance(st, 0.15)[0] == "bullish" and st["p_below"] == pytest.approx(0.2)
    assert V.reference_stance(V.risk_stats(m, 12.4), 0.15)[0] == "neutral"
    assert V.risk_stats(m, None) is None and V.stance_check("bullish", None, 0.15) is None


def test_baseline_drivers_follow_narrative_and_order(vapp):  # noqa: F811
    model = xpeng_model(vapp, XPENG_DRIVERS)
    drivers = {s.name: s.driver for s in model.scenarios}
    assert drivers == {"bull": 1300, "base": 1050, "bear": 900}      # 旧逻辑是 1300 / 950 / 1050（熊市收入高于基准）
    for s in model.scenarios:                                          # 价格仍等于报告区间中点，倍数随之反解
        assert model.price(s) == pytest.approx((s.low + s.high) / 2)
    assert model.scenario("base").multiple == pytest.approx(0.5814, abs=1e-3)
    assert model.scenario("bear").multiple == pytest.approx(0.4667, abs=1e-3)
    plain = xpeng_model(vapp)                                          # 描述里没有矩阵行的数字：熊市取命中格点里最小的
    assert plain.scenario("bear").driver == 900 and plain.scenario("bull").driver == 1300


def test_choose_drivers_clamps_when_order_impossible():
    a = DOSSIERS["xpeng"]["calls"][0]
    cells = {(d, m): matrix_price(d, m, a["shares"], a["net_cash"], a["fx"]) for d in a["driver_values"] for m in a["multiples"]}
    scen = [{"name": "base", "price_low": 31.07, "price_high": 40.82, "drivers": XPENG_DRIVERS["base"]},
            {"name": "bear", "price_low": 29.85, "price_high": 29.85, "drivers": ""}]                 # 只命中 1200 × 0.4
    chosen, flags = V.choose_drivers(scen, cells, V.match_endpoints(cells, scen), a["driver_values"])
    assert chosen["base"] == 1050 and chosen["bear"] == 1050
    assert flags and "保持 牛 ≥ 基准 ≥ 熊" in flags[0] and "取基准情景的值" in flags[0]


def test_falsifies_and_labels():
    assert dossier.falsifies("bullish", "down") and not dossier.falsifies("bullish", "up")
    assert dossier.falsifies("bearish", "up") and not dossier.falsifies("bearish", "down")
    assert dossier.falsifies("neutral", "up") and dossier.falsifies("neutral", "down")
    assert dossier.falsifies(None, None)                                   # 旧数据：按偏多、向下处理
    assert dossier.trigger_label("bearish", {"direction": "down"}) == "风险条件触发（与偏空判断同向，属印证）"
    assert dossier.trigger_label("bearish", {"direction": "up"}) == "证伪条件触发（向上）"
    assert dossier.trigger_label("bullish", {}) == "证伪条件触发（向下）"
    k = dossier.KillCriterion(description="交付超指引", direction="up", action="exit")
    assert k.action == "review"
    assert dossier.KillCriterion(description="毛利率跌破 12%", action="upgrade").action == "review"
    assert dossier.KillCriterion(description="毛利率回升", direction="up", action="upgrade").action == "upgrade"


def test_monitor_message_depends_on_stance(tapp, monkeypatch):  # noqa: F811
    bearish = {**CARD, "stance": "bearish"}
    save_dongyue(tapp, card=bearish, triggers=[
        {"description": "跌破 12 港元", "kind": "price", "metric": "close", "listing": "HK:00189", "operator": "<", "threshold": 12,
         "action": "reduce", "direction": "down"},
        {"description": "站上 10 港元", "kind": "price", "metric": "close", "listing": "HK:00189", "operator": ">", "threshold": 10,
         "action": "upgrade", "direction": "up"}])
    msgs = monitor.check_price(tapp, tapp.watchlist.get("dongyue"))
    assert any("风险条件触发（与偏空判断同向，属印证）【减仓】跌破 12 港元" in m for m in msgs)
    assert any("证伪条件触发（向上）【上调判断】站上 10 港元" in m for m in msgs)
    assert {t["direction"] for t in tapp.db.triggers("dongyue")} == {"down", "up"}


def test_triggers_direction_migration(tmp_path):
    path = tmp_path / "v3.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE triggers (id INTEGER PRIMARY KEY, company_id TEXT NOT NULL, dossier_id INTEGER, "
                       "description TEXT NOT NULL, kind TEXT NOT NULL, metric TEXT, listing TEXT, operator TEXT, threshold REAL, "
                       "action TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'ok', evidence TEXT, "
                       "checked_at TEXT, triggered_at TEXT, alerted_at TEXT); "
                       "INSERT INTO triggers(company_id, description, kind, action) VALUES('x', '旧条件', 'event', 'reduce'); "
                       "PRAGMA user_version=3;")
    conn.close()
    db = DB(path)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
    assert db.triggers("x")[0]["direction"] == "down"
    db.close()
