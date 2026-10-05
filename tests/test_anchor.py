"""估值口径与程序参考价：PE 等股权倍数不再加减净现金；旧报告（统一加净现金）的基线仍按原区间重建；
参考价 = 一致预期 EPS × 历史中位 PE × 汇率，估值矩阵自动补行列，偏离过大时标注；周期股不按季节性外推全年。
夹具数字取自中远海能 2026-10 两份深度报告（H 股定价，人民币报表，净负债 186.01 亿元）。全部离线。"""

from __future__ import annotations

from datetime import date

import pytest

from stockagent import anchor, consensus
from stockagent import valuation as V
from stockagent.sources import emdata
from stockagent.tools import ToolBox, infer_basis, matrix_price, matrix_table

SHARES, NET_CASH, FX = 54.712, -186.01, 1.1704
TODAY = date(2026, 10, 4)


def seed_fx(app) -> None:
    app.db.upsert_bars("133.HKDCNH", [{"date": "2026-10-02", "open": 0.8544, "close": 0.8544, "high": 0.8544, "low": 0.8544,
                                       "volume": 0, "amount": None}])


def fake_anchor(app, monkeypatch) -> dict:
    """历史 PE 三段（25/50/75 分位 7.0/9.5/12.0），2027 年一致预期 EPS 2.50 元（15 家）与研报库 2.00 元（2 家）。"""
    seed_fx(app)
    monkeypatch.setattr(anchor, "valuation_series", lambda *a, **k: ([7.0] * 100 + [9.5] * 100 + [12.0] * 100, [], 13.36, None))
    monkeypatch.setattr(emdata, "a_consensus", lambda *a, **k: {
        "years": [{"fy": 2025, "actual": True, "eps": 0.83}, {"fy": 2027, "actual": False, "eps": 2.50}], "orgs": 15})
    monkeypatch.setattr(emdata, "research_reports", lambda *a, **k: [
        {"date": "2026-09-23", "org": "甲证券", "years": {2026: 2.14, 2027: 1.92}},
        {"date": "2026-08-30", "org": "乙证券", "years": {2026: 2.29, 2027: 2.08}}])
    return anchor.compute(app, app.watchlist.get("acme"), today=TODAY)


def test_equity_multiples_ignore_net_cash():
    assert infer_basis("PE(×)") == "equity" and infer_basis("PB") == "equity" and infer_basis("EV/EBITDA") == "ev"
    assert matrix_price(100, 10, SHARES, NET_CASH, FX, "equity") == pytest.approx(100 * 10 / SHARES * FX)
    assert matrix_price(100, 10, SHARES, NET_CASH, FX, "ev") == pytest.approx((1000 + NET_CASH) / SHARES * FX)  # 旧公式：17.41
    t = matrix_table("2027E 净利", [100], "PE", [10], SHARES, NET_CASH, FX, basis="equity")
    assert "21.39" in t and "股权口径" in t and "未计入" in t


def test_anchor_reference_price_and_check(app, monkeypatch):
    anc = fake_anchor(app, monkeypatch)
    y = anchor.primary(anc)
    assert y["fy"] == 2027 and [e["n"] for e in y["estimates"]] == [15, 2]           # 机构多的来源排前面
    assert y["estimates"][1]["eps"] == pytest.approx((1.92 + 2.08) / 2)               # 研报库：每家最新一篇的中位数
    assert y["price"] == pytest.approx(2.50 / 0.8544 * 9.5) and y["low"] == pytest.approx(2.50 / 0.8544 * 7.0)
    pro = anchor.check(anc, 14.415, 0.25)                                             # pro 版基准情景中值
    flash = anchor.check(anc, 22.42, 0.25)
    assert pro["flag"] and pro["gap"] == pytest.approx(14.415 / y["price"] - 1) and "偏离超过 ±25%" in pro["note"]
    assert not flash["flag"] and "9.50 倍" in flash["note"]
    text = anchor.text(anc, 0.25)
    assert "参考价" in text and "27.80" in text and "15 家机构评级" in text
    assert anchor.check(None, 20, 0.25) is None


def test_matrix_tool_adds_consensus_row_and_pe_columns(app, monkeypatch):
    anc = fake_anchor(app, monkeypatch)
    monkeypatch.setattr(anchor, "get", lambda *a, **k: anc)
    box = ToolBox(app, app.watchlist.get("acme"))
    out = box.call("valuation_matrix", {"driver_label": "2027E归母净利(亿CNY)", "driver_values": [80, 90, 100, 110, 120],
                                        "multiple_label": "PE(×)", "multiples": [6, 8, 10, 12], "shares": SHARES,
                                        "net_cash": NET_CASH, "fx": FX, "current_price": 19.15})
    meta = box.log[-1]["meta"]
    assert meta["basis"] == "equity" and 136.8 in meta["driver_values"] and 109.4 not in meta["driver_values"]  # 109.4 与 110 太近
    assert meta["multiples"] == [6, 7.0, 8, 9.5, 10, 12]
    assert "程序已补上驱动行 136.8" in out and "程序已补上倍数列 7×、9.5×" in out and "程序参考价" in out
    assert "| 136.8 |" in out and "股权口径" in out


def test_baseline_uses_augmented_grid_and_equity_basis(app, monkeypatch):
    anc = fake_anchor(app, monkeypatch)
    monkeypatch.setattr(anchor, "get", lambda *a, **k: anc)
    company = app.watchlist.get("acme")
    box = ToolBox(app, company)
    box.call("valuation_matrix", {"driver_label": "2027E归母净利(亿CNY)", "driver_values": [80, 100, 120],
                                  "multiple_label": "PE(×)", "multiples": [6, 8, 10, 12], "shares": SHARES, "net_cash": NET_CASH,
                                  "fx": FX})
    cell = lambda d, m: round(matrix_price(d, m, SHARES, NET_CASH, FX, "equity"), 2)  # noqa: E731
    scen = [("bull", cell(120, 12), cell(136.8, 12), 0.25), ("base", cell(100, 9.5), cell(136.8, 9.5), 0.5),
            ("bear", cell(80, 7.0), cell(80, 9.5), 0.25)]                             # 都用到了程序补上的行列
    card = {"thesis": "论点", "stance": "neutral", "price_listing": "HK:01234", "anchor": anc,
            "scenarios": [{"name": n, "price_low": lo, "price_high": hi, "probability": p, "drivers": ""} for n, lo, hi, p in scen]}
    app.db.save_dossier("acme", "mock", "# 报告", card, {}, box.log, [], [], {})
    app.db.save_quote("116.01234", {"price": 19.15, "market_cap": 19.15 * SHARES * 1e8})
    model, flags = V.build_baseline(app, company, app.db.latest_dossier("acme"))
    assert model.method == "matrix" and model.basis == "equity" and "命中 6/6" in model.source
    for name, lo, hi, _ in scen:
        assert model.price(model.scenario(name)) == pytest.approx((lo + hi) / 2)
    assert "g.net_cash" not in V.paths(model) and model.anchor == anc
    assert "股权口径" in V.describe(model) and "程序参考价" in V.describe(model, price=19.15)


def test_legacy_pe_matrix_with_net_cash_keeps_report_prices(app):
    """区分口径之前的报告：PE 矩阵也加了净现金，卡片区间按旧公式算出；重建基线时按旧公式还原，并提示重跑。"""
    company = app.watchlist.get("acme")
    args = {"driver_label": "2027E归母净利(亿CNY)", "driver_values": [80, 90, 100, 110, 120], "multiple_label": "PE(×)",
            "multiples": [6, 8, 10, 12], "shares": 54.71, "net_cash": NET_CASH, "fx": FX, "current_price": 19.15}
    log = [{"tool": "valuation_matrix", "args": args, "result": matrix_table(
        args["driver_label"], args["driver_values"], args["multiple_label"], args["multiples"], 54.71, NET_CASH, FX)}]
    scen = [("bull", 19.55, 26.83, 0.25), ("base", 11.42, 17.41, 0.5), ("bear", 6.29, 9.71, 0.25)]  # pro 版报告原样
    card = {"thesis": "论点", "stance": "neutral", "price_listing": "HK:01234",
            "scenarios": [{"name": n, "price_low": lo, "price_high": hi, "probability": p, "drivers": ""} for n, lo, hi, p in scen]}
    app.db.save_dossier("acme", "mock", "# 报告", card, {}, log, [], [], {})
    model, flags = V.build_baseline(app, company, app.db.latest_dossier("acme"))
    assert model.basis == "ev" and model.target() == pytest.approx(15.005, abs=0.01)  # 与当时的基线一致
    assert any("旧算法" in f for f in flags) and "g.net_cash" in V.paths(model)
    assert V.ValuationModel.model_validate({k: v for k, v in model.model_dump().items() if k != "basis"}).basis == "ev"


def test_unstable_seasonality_is_not_extrapolated():
    """油运这类周期股：中报占全年比例在 47%–83% 之间跳，只给完成率与历史区间，不给外推偏差。"""
    def row(period: str, eps: float) -> dict:
        return {"period_end": period, "notice": None, "eps": eps, "profit": eps * 54.7e8, "revenue": None, "currency": "CNY"}
    history = [row("2026-06-30", 0.83), row("2025-12-31", 0.83), row("2025-06-30", 0.39), row("2024-12-31", 0.92),
               row("2024-06-30", 0.76), row("2023-12-31", 0.62), row("2023-06-30", 0.40)]
    cons = {"fy": 2026, "metric": "eps", "value": 2.14, "n": 1, "low": None, "high": None, "as_of": "2026-07-12",
            "currency": "CNY", "source": "测试", "flags": []}
    res = consensus.compare(history[0], cons, history, "report")
    assert res["completion"] == pytest.approx(0.83 / 2.14) and "implied_gap" not in res
    assert "外推" not in res["headline"] and "近 3 年同期占比 47%–83%" in res["headline"]
    assert any("不外推全年" in f for f in res["flags"])


# ---------------------------------------------------------------- 美股定价（含 ADS）

def us_company(app, ads_ratio=None, per_ads=False):
    from stockagent.config import Company, Listing

    co = Company(id="usco", name="美股公司", listings=[Listing(market="US", code="USCO", exchange="NYSE", ads_ratio=ads_ratio)])
    app.watchlist.companies.append(co)
    # Nasdaq 一致预期快照：2027 年 EPS 4.00 美元（per_ads 时为每 ADS 口径）
    app.db.save_consensus("usco", "nasdaq", "2026-10-03", [{"fiscal_year": 2027, "metric": "eps", "value": 4.0, "currency": "USD",
                                                           "per_ads": per_ads, "n": 6}])
    return co


def test_anchor_us_ordinary_shares(app, monkeypatch):
    co = us_company(app)
    monkeypatch.setattr(anchor, "valuation_series", lambda *a, **k: ([20.0] * 100 + [30.0] * 100 + [40.0] * 100, [], 28.0, None))
    anc = anchor.compute(app, co, today=TODAY)
    y = anchor.primary(anc)
    assert anc["listing"] == "US:USCO" and anc["currency"] == "USD" and anc["ads_ratio"] == 1.0
    assert y["fy"] == 2027 and y["estimates"][0]["fx"] == 1.0 and y["estimates"][0]["n"] == 6
    assert y["price"] == pytest.approx(4.0 * 30.0) and y["low"] == pytest.approx(80) and y["high"] == pytest.approx(160)
    assert "每 ADS" not in anchor.text(anc, 0.25)


def test_anchor_us_ads_price_uses_ads_ratio(app, monkeypatch):
    # Nasdaq 给的是每 ADS 4.00 美元；换算成每股 2.00（ads_ratio=2），参考价按每股 EPS × 2（每 ADS 股数）× PE 回到 ADS 口径
    co = us_company(app, ads_ratio=2, per_ads=True)
    monkeypatch.setattr(anchor, "valuation_series", lambda *a, **k: ([20.0] * 100 + [30.0] * 100 + [40.0] * 100, [], 28.0, None))
    anc = anchor.compute(app, co, today=TODAY)
    y = anchor.primary(anc)
    assert y["estimates"][0]["eps"] == pytest.approx(2.0) and anc["ads_ratio"] == 2
    assert y["price"] == pytest.approx(2.0 * 2 * 30.0)  # = 每 ADS EPS 4.00 × 30 倍
    text = anchor.text(anc, 0.25)
    assert "每 ADS 2 股" in text and "120" in text
    # 矩阵：PE 分位列照常补；一致预期净利行因为股本口径含糊不补，并说明
    drivers, multiples, notes = anchor.augment(anc, "2027E归母净利(亿USD)", [10, 20], "PE(×)", [15, 25], 5.0, 1.0, "equity")
    assert drivers == [10, 20] and 20.0 in multiples and any("无法确认" in n for n in notes)


def test_anchor_us_ads_without_ratio_and_non_usd_reports_has_no_pe(app, monkeypatch):
    from stockagent import tools, usdata
    from stockagent.config import Listing

    # 报表为人民币的 ADS 发行人没填 ads_ratio：不知道每 ADS 几股，历史 PE 不给（避免价格与每股 EPS 口径错位）
    monkeypatch.setattr(usdata, "load_facts", lambda a, l: ({"x": 1}, ""))
    monkeypatch.setattr(usdata, "eps_bps_points", lambda f: ([("2025-01-01", 1.0, 2.0)], "CNY"))
    assert tools.us_valuation_series(app, Listing(market="US", code="KC", exchange="NASDAQ"), 3) == ([], [], None, None)


def test_us_pe_ignores_near_zero_earnings(app, monkeypatch):
    import pandas as pd

    from stockagent import tools, usdata
    from stockagent.config import Listing

    monkeypatch.setattr(usdata, "load_facts", lambda a, l: ({"x": 1}, ""))
    # 2025 年 TTM EPS 只有 0.05（价格 100 → PE 2000 倍，不是估值信号），2026 年 EPS 4.0（PE 25 倍）
    monkeypatch.setattr(usdata, "eps_bps_points", lambda f: ([("2025-01-01", 0.05, 2.0), ("2026-01-01", 4.0, 2.5)], "USD"))
    dates = [f"2025-{m:02d}-15" for m in range(2, 13)] + [f"2026-{m:02d}-15" for m in range(2, 10)]
    bars = pd.DataFrame({"date": dates, "close": [100.0] * len(dates)})
    monkeypatch.setattr(tools, "ensure_bars", lambda *a, **k: bars)
    l = Listing(market="US", code="USCO", exchange="NYSE")
    pe, pb, cur_pe, cur_pb = tools.us_valuation_series(app, l, 3)
    assert pe and all(v == pytest.approx(25.0) for v in pe) and cur_pe == pytest.approx(25.0) and cur_pb == pytest.approx(40.0)
