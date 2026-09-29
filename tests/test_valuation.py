"""估值模型：汇率换算、由深度报告的估值矩阵在代码中重建基线（夹具取自真实报告的工具参数与跟踪卡）。"""

from __future__ import annotations

import textwrap
from datetime import date

import pytest

from stockagent import valuation as V
from stockagent.app import App
from stockagent.tools import matrix_price, matrix_table

WATCHLIST = """
companies:
  - id: dongyue
    name: 东岳集团
    listings: [{market: HK, code: "00189"}]
  - id: innoscience
    name: 英诺赛科
    listings: [{market: HK, code: "02577"}]
  - id: innolight
    name: 中际旭创
    listings: [{market: A, code: "300308"}, {market: HK, code: "03308"}]
  - id: minimax
    name: MiniMax
    listings: [{market: HK, code: "00100"}]
  - id: xpeng
    name: 小鹏汽车
    listings: [{market: HK, code: "09868"}, {market: US, code: XPEV, exchange: NYSE}]
"""

# 真实报告（2026-09-28）的跟踪卡情景与 valuation_matrix 参数
DOSSIERS = {
    "dongyue": {
        "scenarios": [("bull", 21.88, 25.12, 0.25), ("base", 13.79, 17.56, 0.5), ("bear", 9.12, 10.55, 0.25)],
        "calls": [{"driver_label": "2027E 归母净利（亿 CNY）", "driver_values": [16, 18, 20, 22, 24, 26], "multiple_label": "PE",
                   "multiples": [6, 8, 10, 12, 14], "shares": 17.33, "net_cash": 60.37, "fx": 1.169, "current_price": 11.05}],
        "quote": ("116.00189", 11.05, 19146460000.0),
    },
    "innoscience": {
        "scenarios": [("bull", 50.86, 68.75, 0.25), ("base", 35.53, 40.64, 0.55), ("bear", 15.09, 18.93, 0.2)],
        "calls": [
            {"driver_label": "2027E 收入（亿元 CNY）", "driver_values": [20, 25, 30, 35, 40, 45], "multiple_label": "EV/Sales（x）",
             "multiples": [4, 6, 8, 10, 12], "net_cash": -1.84, "shares": 915100653, "fx": 1.169, "current_price": 37.36},
            {"driver_label": "2027E 收入（亿元 CNY）", "driver_values": [20, 25, 30, 35, 40, 45], "multiple_label": "EV/Sales（x）",
             "multiples": [4, 6, 8, 10, 12], "net_cash": -1.84, "shares": 9.15100653, "fx": 1.169, "current_price": 37.36}],
        "quote": ("116.02577", 37.36, 34188160000.0),
    },
    "innolight": {
        "scenarios": [("bull", 1321.87, 1491.67, 0.25), ("base", 795.52, 953.42, 0.5), ("bear", 362.55, 579.03, 0.25)],
        "calls": [{"driver_label": "2027E归母净利(亿元)", "driver_values": [350, 450, 550, 620, 700], "multiple_label": "PE",
                   "multiples": [12, 15, 18, 20, 25], "shares": 11.7791, "net_cash": 70.48, "fx": 1, "current_price": 815}],
        "quote": ("0.300308", 815.0, 959995999999.9999),
    },
    "minimax": {
        "scenarios": [("bull", 476.46, 557.33, 0.25), ("base", 269.8, 296.76, 0.5), ("bear", 126.04, 144.01, 0.25)],
        "calls": [
            {"driver_label": "2026E收入（亿美元）", "driver_values": [3.0, 4.0, 5.0, 6.0], "multiple_label": "EV/Sales",
             "multiples": [8, 12, 16, 20, 25], "net_cash": 32.11, "shares": 3.492353, "fx": 7.8449, "current_price": 250.2},
            {"driver_label": "2027E收入（亿美元）", "driver_values": [8.0, 11.0, 14.6, 18.0], "multiple_label": "EV/Sales",
             "multiples": [4, 6, 8, 10, 12], "net_cash": 32.11, "shares": 3.492353, "fx": 7.8449, "current_price": 250.2}],
        "quote": ("116.00100", 250.2, 87378670000.0),
    },
    "xpeng": {
        "scenarios": [("bull", 64.0, 79.8, 0.2), ("base", 31.1, 44.5, 0.45), ("bear", 22.5, 29.9, 0.35)],
        "calls": [
            {"driver_label": "2027E 营业收入（亿元人民币）", "driver_values": [900, 1000, 1100, 1200, 1300], "multiple_label": "P/S",
             "multiples": [0.4, 0.5, 0.6, 0.7, 0.8, 1.0], "shares": 19.19, "net_cash": 10.03, "fx": 1.169, "current_price": 39.1},
            {"driver_label": "2028E 归母净利润（亿元人民币）", "driver_values": [10, 15, 22.66, 30, 40], "multiple_label": "PE",
             "multiples": [15, 20, 25, 30, 40], "net_cash": 10.03, "shares": 19.19, "fx": 1.169, "current_price": 39.1}],
        "quote": ("116.09868", 39.1, 75020170000.0),
    },
}


def seed_fx(app, day_close: dict[str, tuple[float, float]]) -> None:
    """day → (HKDCNH, USDCNH) 收盘价。"""
    for secid, k in (("133.HKDCNH", 0), ("133.USDCNH", 1)):
        app.db.upsert_bars(secid, [{"date": d, "open": v[k], "close": v[k], "high": v[k], "low": v[k], "volume": 0, "amount": None}
                                   for d, v in day_close.items()])


def save_dossier(app, cid: str, spec: dict, created_at: str = "2026-09-28T12:00:00+00:00") -> dict:
    card = {"thesis": "论点", "scenarios": [{"name": n, "price_low": lo, "price_high": hi, "probability": p, "drivers": f"{n} 驱动"}
                                           for n, lo, hi, p in spec["scenarios"]]}
    tool_log = [{"tool": "fcf_yield", "args": {}, "result": "EV …"}] + [
        {"tool": "valuation_matrix", "args": a, "result": matrix_table(
            a["driver_label"], a["driver_values"], a["multiple_label"], a["multiples"], a["shares"], a["net_cash"], a["fx"])}
        for a in spec["calls"]]
    did = app.db.save_dossier(cid, "mock", "# 报告", card, {}, tool_log, [], [], {})
    app.db._exec("UPDATE dossiers SET created_at=? WHERE id=?", (created_at, did))
    if "quote" in spec:
        secid, price, mcap = spec["quote"]
        app.db.save_quote(secid, {"price": price, "market_cap": mcap})
    return app.db.latest_dossier(cid)


@pytest.fixture
def vapp(project) -> App:
    (project / "watchlist.yaml").write_text(textwrap.dedent(WATCHLIST), encoding="utf-8")
    a = App.load(project / "config.yaml")
    seed_fx(a, {"2026-09-25": (0.85584, 6.7132), "2026-09-28": (0.85541, 6.7105)})
    yield a
    a.db.close()


def test_fx_cross_rates(vapp):
    on = date(2026, 9, 28)
    assert V.fx_rate(vapp, "CNY", "HKD", on)[0] == pytest.approx(1 / 0.85541)        # 1.169（报告人民币 → 港股股价）
    assert V.fx_rate(vapp, "USD", "HKD", on)[0] == pytest.approx(6.7105 / 0.85541)   # 7.8448
    assert V.fx_rate(vapp, "HKD", "CNY", on) == (pytest.approx(0.85541), "2026-09-28")
    assert V.fx_rate(vapp, "CNY", "CNY", on)[0] == 1.0
    assert V.fx_rate(vapp, "CNY", "HKD", date(2026, 9, 26))[1] == "2026-09-25"       # 取当日或之前最近一天
    assert V.fx_rate(vapp, "CNY", "HKD", date(2026, 10, 10)) is None                  # 超过 5 天视为陈旧
    assert V.fx_rate(vapp, "EUR", "HKD", on) is None


def test_model_price_matches_matrix_tool():
    a = DOSSIERS["dongyue"]["calls"][0]
    m = V.ValuationModel(driver_label=a["driver_label"], multiple_label="PE", shares=a["shares"], net_cash=a["net_cash"],
                         fx=a["fx"], report_currency="CNY", price_currency="HKD", listing="HK:00189",
                         scenarios=[V.ScenarioPoint(name="base", driver=20, multiple=10, probability=1)])
    assert m.price(m.scenarios[0]) == matrix_price(20, 10, a["shares"], a["net_cash"], a["fx"])
    assert f"{m.price(m.scenarios[0]):,.2f}" == "17.56"  # 与报告矩阵的格点一致
    assert "17.56" in matrix_table(a["driver_label"], [20], "PE", [10], a["shares"], a["net_cash"], a["fx"])


@pytest.mark.parametrize("cid,ccy,index", [("dongyue", "CNY", 2), ("innoscience", "CNY", 3), ("innolight", "CNY", 2),
                                           ("minimax", "USD", 3), ("xpeng", "CNY", 2)])
def test_baseline_reproduces_card_midpoints(vapp, cid, ccy, index):
    spec = DOSSIERS[cid]
    model, flags = V.build_baseline(vapp, vapp.watchlist.get(cid), save_dossier(vapp, cid, spec))
    assert model.method == "matrix" and model.report_currency == ccy
    assert f"第 {index} 次工具调用" in model.source
    for name, lo, hi, _ in spec["scenarios"]:
        assert model.price(model.scenario(name)) == pytest.approx((lo + hi) / 2)
    assert sum(s.probability for s in model.scenarios) == pytest.approx(1)
    weights = {n: p for n, *_, p in spec["scenarios"]}
    expect = sum(weights[n] * (lo + hi) / 2 for n, lo, hi, _ in spec["scenarios"]) / sum(weights.values())
    assert model.target() == pytest.approx(expect) and model.target("base") == pytest.approx(model.price(model.scenario("base")))
    assert not any("股本" in f for f in flags)


def test_baseline_details(vapp):
    model, _ = V.build_baseline(vapp, vapp.watchlist.get("dongyue"), save_dossier(vapp, "dongyue", DOSSIERS["dongyue"]))
    base = model.scenario("base")
    assert base.driver == 19 and base.multiple == pytest.approx(9.05, abs=0.01)  # 18×8 与 20×10 命中，驱动取均值后反解倍数
    assert model.scenario("bear").driver == 16                                   # 9.12 为 250 日低点，只命中 16×6
    assert "命中 5/6" in model.source
    # 股本写成股数的那次调用被跳过，改用第二次
    model, flags = V.build_baseline(vapp, vapp.watchlist.get("innoscience"), save_dossier(vapp, "innoscience", DOSSIERS["innoscience"]))
    assert model.shares == pytest.approx(9.15100653) and "命中 6/6" in model.source


def test_baseline_falls_back_to_range_model(vapp):
    spec = {"scenarios": DOSSIERS["dongyue"]["scenarios"], "calls": [], "quote": DOSSIERS["dongyue"]["quote"]}
    model, flags = V.build_baseline(vapp, vapp.watchlist.get("dongyue"), save_dossier(vapp, "dongyue", spec))
    assert model.method == "range" and any("区间模型" in f for f in flags)
    assert model.price(model.scenario("base")) == pytest.approx((13.79 + 17.56) / 2)
    assert "g.shares" not in V.paths(model) and "base.driver" in V.paths(model)
    # 股本与行情不符的矩阵不采用
    bad = {**DOSSIERS["dongyue"], "calls": [{**DOSSIERS["dongyue"]["calls"][0], "shares": 1733}]}
    model, flags = V.build_baseline(vapp, vapp.watchlist.get("dongyue"), save_dossier(vapp, "dongyue", bad))
    assert model.method == "range" and any("不符" in f for f in flags)


def test_paths_and_ops():
    m = V.ValuationModel(driver_label="2027E 净利", multiple_label="PE", shares=10, net_cash=5, fx=1, price_currency="CNY",
                         listing="A:300001", scenarios=[V.ScenarioPoint(name="base", driver=10, multiple=8, probability=0.6),
                                                        V.ScenarioPoint(name="bear", driver=8, multiple=6, probability=0.6)])
    assert m.probabilities() == {"base": 0.5, "bear": 0.5}
    assert m.price(m.scenario("base")) == pytest.approx(8.5)
    assert V.paths(m) == ["base.driver", "bear.driver", "all.driver", "base.multiple", "bear.multiple", "all.multiple",
                          "g.shares", "g.net_cash", "prob.base", "prob.bear"]
    assert V.get_values(m, "all.driver") == [("base.driver", 10), ("bear.driver", 8)]
    V.set_value(m, "base.driver", V.apply_op(10, "mul", 1.1))
    assert m.scenario("base").driver == pytest.approx(11)
    assert V.apply_op(5, "add", -2) == 3 and V.apply_op(5, "set", 7) == 7
    assert V.check_value(m, "base.multiple", 0) and V.check_value(m, "g.shares", -1) and V.check_value(m, "prob.base", 1.5)
    assert V.check_value(m, "g.net_cash", -100) is None and V.check_value(m, "base.driver", float("inf"))
    # 负价截为 0
    V.set_value(m, "g.net_cash", -1000)
    assert m.price(m.scenario("base")) == 0
    assert V.TARGET_LABEL in V.describe(m) and "基准·2027E 净利" == V.path_label(m, "base.driver")
