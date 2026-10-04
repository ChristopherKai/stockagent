"""模拟基金：配置、交易日历、成本与取整、风控上限与熔断、账本、两个策略（合成日线）、端到端流程。全部离线。"""

from __future__ import annotations

import json
import textwrap
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
import yaml

from stockagent import cli, jobs
from stockagent.fund import calendar, costs, data, engine, ledger, report, risk, store
from stockagent.fund import config as fcfg
from stockagent.fund.strategy import (
    ASectorTrend,
    Context,
    ExitSignal,
    HKQualityValue,
    Target,
)
from stockagent.screen import store as sc_store
from stockagent.utils import CST, today_local

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 合成数据


def trading_days(market: str, end: date, n: int) -> list[date]:
    out = [end]
    while len(out) < n:
        out.append(calendar.prev_trading_day(market, out[-1]))
    return out[::-1]


def bar_rows(days: list[date], closes, volumes=None, market: str = "A") -> list[dict]:
    lot = 100 if market == "A" else 1
    rows = []
    for i, (d, c) in enumerate(zip(days, closes)):
        v = float(volumes[i]) if volumes is not None else 1e6
        rows.append({"date": d.isoformat(), "open": c * 0.995, "close": float(c), "high": c * 1.01, "low": c * 0.99,
                     "volume": v, "amount": v * c * lot, "turnover": None})
    return rows


def trend_closes(n: int, growth: float, base: float = 10.0, consolidate: int = 20, breakout: float | None = 1.02) -> np.ndarray:
    """稳定上涨，最后 consolidate 天在高点下方 3% 内横盘；breakout 给出时最后一天收在横盘高点之上。"""
    c = base * (1 + growth) ** np.arange(n)
    top = c[n - consolidate - 1]
    c[n - consolidate:] = top * (0.97 + 0.02 * (np.arange(consolidate) % 2))
    if breakout:
        c[-1] = c[-consolidate - 1:-1].max() * 1.01 * breakout
    return c


def noisy_index(n: int, seed: int = 7) -> np.ndarray:
    rng = np.random.RandomState(seed)
    r = rng.normal(0.0008, 0.012, n)
    r[-25:] = rng.normal(0.001, 0.003, 25)          # 最近波动小：波动率分位低
    return 3000 * np.cumprod(1 + r)


def a_row(code: str, name: str, industry: str, mcap: float = 2e10, price: float = 20.0) -> dict:
    sid = f"{'1' if code.startswith('6') else '0'}.{code}"
    return {"secid": sid, "market": "A", "code": code, "name": name, "org_code": f"ORG{code}", "industry": industry,
            "org_type": "一般企业", "st": 0, "listing_date": None, "price": price, "mcap": mcap, "currency": "CNY",
            "report_ccy": "CNY", "pe_ttm": 20.0, "pb": 2.0, "quote_date": "2026-09-30", "info": {"roe": 15.0}}


def hk_flows(y: int, ocf: float = 60e8, capex: float = 20e8) -> list[dict]:
    return [{"date": f"{y}-06-30", "annual": False, "ocf": ocf / 2, "capex": capex / 2},
            {"date": f"{y - 1}-12-31", "annual": True, "ocf": ocf, "capex": capex},
            {"date": f"{y - 1}-06-30", "annual": False, "ocf": ocf / 2, "capex": capex / 2},
            {"date": f"{y - 2}-12-31", "annual": True, "ocf": ocf * 0.9, "capex": capex},
            {"date": f"{y - 3}-12-31", "annual": True, "ocf": ocf * 0.8, "capex": capex}]


def hk_row(code: str, name: str, roe: float, pe: float, industry: str = "食品", mcap: float = 400e8, ocf: float = 60e8,
           debt_ratio: float = 30.0) -> dict:
    y = today_local().year
    bal = {"date": f"{y}-06-30", "cash": 50e8, "cash_items": {}, "invest": 0.0, "debt": 10e8, "debt_items": {}, "minority": 0.0,
           "liabilities": 80e8, "restricted": 0.0}
    return {"secid": f"116.{code}", "market": "HK", "code": code, "name": name, "org_code": f"HKORG{code}", "industry": industry,
            "org_type": "一般企业", "st": 0, "listing_date": "2010-01-01", "price": 20.0, "mcap": mcap, "currency": "HKD",
            "report_ccy": "HKD", "pe_ttm": pe, "pb": 1.5, "quote_date": "2026-09-30",
            "info": {"roe": roe, "debt_ratio": debt_ratio, "dividend_yield": 0.05, "payout": 0.4, "gross_margin": 40.0,
                     "gross_margin_prev": 41.0, "report_date": f"{y}-06-30"},
            "fcf": {"flows": hk_flows(y, ocf), "balance": bal}}


def seed_universe(app, rows: list[dict]) -> None:
    for m in ("HK", "A"):
        rs = [r for r in rows if r["market"] == m]
        if rs:
            sc_store.upsert_universe(app.db, rs, m)
    sc_store.set_fcf(app.db, {r["secid"]: r["fcf"] for r in rows if r.get("fcf")})


FUNDS_YAML = """
funds:
  - {id: hk_value, name: 港股质量价值, market: HK, strategy: hk_quality_value, style: value, currency: HKD,
     max_holdings: 15, rebalance: quarterly, benchmark: "100.HSI"}
  - {id: a_trend, name: A 股景气趋势, market: A, strategy: a_sector_trend, style: trend, currency: CNY,
     max_holdings: 10, rebalance: weekly, benchmark: "1.000300", params: {rs_top: 0.3}}
universe:
  min_mcap: {HK: 1.0e9, A: 1.0e9}
  min_turnover: {HK: 1.0e6, A: 1.0e6}
regime:
  min_breadth_n: 5
"""


def as_of_date() -> date:
    """最近一个港股、A 股都开市、且前一个交易日也相同的日子（合成日线以它的前一交易日为最新收盘）。"""
    d = today_local() - timedelta(days=1)
    while True:
        if calendar.is_trading_day("A", d) and calendar.is_trading_day("HK", d) and \
                calendar.prev_trading_day("A", d) == calendar.prev_trading_day("HK", d):
            return d
        d -= timedelta(days=1)


# ---------------------------------------------------------------- 配置


def test_repo_funds_yaml_is_valid():
    cfg = fcfg.FundsFile.model_validate(yaml.safe_load((ROOT / "funds.yaml").read_text(encoding="utf-8")))
    assert {f.id for f in cfg.funds} == {"hk_quality_value", "a_sector_trend"}
    hk = cfg.fund("hk_quality_value")
    assert hk.max_holdings == 15 and hk.max_weight == 0.15 and hk.drawdown == (0.05, 0.10, 0.15) and hk.initial_capital == 1e6
    assert cfg.fund("a_sector_trend").max_holdings == 10
    assert cfg.cost("HK").stamp_buy == 0.001 and cfg.cost("A").stamp_buy == 0 and cfg.cost("A").stamp_sell == 0.0005


@pytest.mark.parametrize("patch, msg", [
    ({"currency": "CNY"}, "币种"),
    ({"strategy": "nope"}, "未知策略"),
    ({"strategy": "a_sector_trend"}, "只适用于"),
    ({"drawdown": [0.1, 0.05, 0.15]}, "递增"),
    ({"params": {"bogus": 1}}, "bogus"),
])
def test_fund_definition_validation(patch, msg):
    base = {"id": "x_fund", "name": "x", "market": "HK", "strategy": "hk_quality_value", "style": "value", "currency": "HKD",
            "max_holdings": 10, "rebalance": "monthly", "benchmark": "100.HSI"}
    with pytest.raises(ValueError, match=msg):
        fcfg.FundDef.model_validate({**base, **patch})


def test_sync_creates_updates_and_refuses_capital_change(app):
    cfg = fcfg.FundsFile.model_validate(yaml.safe_load(FUNDS_YAML))
    assert any("新建 hk_value" in m for m in store.sync(app.db, cfg.funds, "2026-10-05", dry_run=True))
    assert store.funds(app.db) == []
    store.sync(app.db, cfg.funds, "2026-10-05")
    f = store.fund(app.db, "hk_value")
    assert f["cash"] == 1e6 and f["inception"] == "2026-10-05" and f["breaker"] == 0
    store.set_fund(app.db, "hk_value", cash=123.0)
    cfg.funds[0].max_holdings = 12
    assert any("更新 hk_value：max_holdings" in m for m in store.sync(app.db, cfg.funds, "2026-10-06"))
    assert store.fund(app.db, "hk_value")["cash"] == 123.0          # 同步不动现金
    cfg.funds[0].initial_capital = 2e6
    assert any("不能修改" in m for m in store.sync(app.db, cfg.funds, "2026-10-06"))
    assert store.fund(app.db, "hk_value")["initial_capital"] == 1e6


# ---------------------------------------------------------------- 交易日历


def test_calendar_holidays_and_navigation():
    assert not calendar.is_trading_day("HK", date(2026, 10, 1)) and calendar.is_trading_day("HK", date(2026, 10, 2))
    assert not calendar.is_trading_day("A", date(2026, 10, 2)) and not calendar.is_trading_day("A", date(2026, 10, 7))
    assert calendar.next_trading_day("A", date(2026, 9, 30)) == date(2026, 10, 8)
    assert calendar.prev_trading_day("A", date(2026, 10, 8)) == date(2026, 9, 30)
    assert not calendar.is_trading_day("HK", date(2027, 9, 16))          # 中秋节翌日（政府公布的 2027 年假期）
    assert not calendar.is_trading_day("HK", date(2027, 2, 9)) and calendar.is_trading_day("HK", date(2027, 2, 10))
    assert calendar.is_half_day("HK", date(2026, 12, 24))
    # 调仓日：A 股第四季度第一个交易日是国庆后的 10-08，港股是 10-02
    assert calendar.first_of_period("A", date(2026, 10, 8), "quarterly")
    assert calendar.first_of_period("HK", date(2026, 10, 2), "quarterly")
    assert not calendar.first_of_period("HK", date(2026, 10, 5), "quarterly")
    assert calendar.first_of_period("HK", date(2026, 10, 5), "weekly")
    assert calendar.trading_days_between("A", date(2026, 9, 28), date(2026, 10, 9)) == 4
    # 2026 年两个市场都已核对；A 股 2027 年还没公布
    assert calendar.note("A", date(2026, 5, 6)) is None and calendar.note("HK", date(2027, 3, 1)) is None
    assert "2027" in calendar.note("A", date(2027, 3, 1))
    for m, years in calendar.HOLIDAYS.items():
        for y, ds in years.items():
            assert all(date.fromisoformat(d).year == y and date.fromisoformat(d).weekday() < 5 for d in ds), (m, y)


# ---------------------------------------------------------------- 成本、取整、涨跌停


def test_costs_lots_and_limits():
    hk, a = fcfg.DEFAULT_COSTS["HK"], fcfg.DEFAULT_COSTS["A"]
    # 港股：印花税 0.1% 向上取整到元；佣金有最低额
    assert costs.fees(hk, "buy", 10_050) == pytest.approx(5 + 11 + 10_050 * (0.000085 + 0.000042), abs=0.01)
    assert costs.fees(hk, "sell", 1_000_000) == pytest.approx(300 + 1000 + 1_000_000 * 0.000127, abs=0.01)
    # A 股：只有卖出收印花税
    assert costs.fees(a, "buy", 100_000) == pytest.approx(25 + 100_000 * 0.0000641, abs=0.01)
    assert costs.fees(a, "sell", 100_000) == pytest.approx(25 + 50 + 100_000 * 0.0000641, abs=0.01)
    assert costs.fill_price(a, "buy", 10.0) == pytest.approx(10.005) and costs.fill_price(a, "sell", 10.0) == pytest.approx(9.995)
    assert costs.lot_size("HK", "00005", 400) == (400, True) and costs.lot_size("HK", "00005") == (100, False)
    assert costs.round_shares("HK", "00005", 1599, 400) == 1200
    assert costs.round_shares("A", "600519", 199, 100) == 100
    assert costs.round_shares("A", "688981", 150, 100) == 0 and costs.round_shares("A", "688981", 260, 100) == 200
    assert costs.limit_prices("600000", 10.0) == (11.0, 9.0)
    assert costs.limit_prices("300750", 10.0) == (12.0, 8.0)
    assert costs.limit_prices("600001", 10.0, "*ST某某") == (10.5, 9.5)


def test_tencent_quote_parses_hk_lot(monkeypatch):
    from stockagent.sources import tencent

    f = ["100"] + ["0"] * 77
    f[1], f[2], f[3], f[4], f[32], f[45], f[60] = "汇丰控股", "00005", "100.0", "99.0", "1.0", "17000", "400"

    class R:
        content = ('v_hk00005="' + "~".join(f) + '";').encode("gbk")

        def raise_for_status(self):
            pass

    class S:
        def get(self, *a, **k):
            return R()

    q = tencent.fetch_quote(S(), "116.00005")
    assert q["lot"] == 400 and q["price"] == 100.0


# ---------------------------------------------------------------- 回撤熔断


@pytest.mark.parametrize("cur, dd, want", [
    (0, 0.04, 0), (0, 0.05, 1), (0, 0.11, 2), (0, 0.16, 3),
    (1, 0.04, 1), (1, 0.029, 0),                 # 一档：回撤收窄到 3% 以内才解除
    (2, 0.07, 2), (2, 0.045, 1), (2, 0.02, 0),   # 二档：5% 以内降档
    (3, 0.0, 3),                                 # 三档只能手动恢复
])
def test_breaker_hysteresis(cur, dd, want):
    assert risk.breaker_level(cur, dd, (0.05, 0.10, 0.15), (0.03, 0.05)) == want


# ---------------------------------------------------------------- 风控


def fund_row(**kw) -> dict:
    return {"id": "f", "name": "测试", "market": "A", "style": "trend", "currency": "CNY", "initial_capital": 1e6,
            "max_holdings": 10, "max_weight": 0.15, "sector_cap": 0.30, "min_weight": 0.03, "status": "paper",
            "dd1": 0.05, "dd2": 0.10, "dd3": 0.15, **kw}


def tgt(code: str, weight: float, score: float = 80.0, sector: str = "电子", price: float = 10.0, tranches: int = 1,
        entry: dict | None = None, org: str | None = None, stop: float | None = 9.0) -> Target:
    return Target(secid=f"0.{code}", code=code, name=f"股{code}", market="A", score=score, weight=weight, price=price, stop=stop,
                  entry=entry or {"type": "open"}, tranches=tranches, reason="测试", sector=sector, org_code=org)


def pos(code: str, shares: float, cost: float = 10.0, sector: str = "电子", score: float = 50.0, stop: float | None = None) -> dict:
    return {"secid": f"0.{code}", "code": code, "name": f"股{code}", "shares": shares, "avg_cost": cost, "entry_price": cost,
            "sector": sector, "score": score, "stop": stop, "opened_on": "2026-09-01", "tranches": 1, "flags": {}}


def limits(**kw) -> risk.Limits:
    return risk.Limits(**{"regime_cap": 1.0, "breaker": 0, "new_entries": None, "rebalance_day": True, **kw})


RC = fcfg.RiskConfig()


def test_single_name_sector_liquidity_and_min_weight_caps():
    book = risk.Book(fund=fund_row(), cash=1e6, positions={}, prices={})
    targets = [tgt("000001", 0.25, 90, "电子"), tgt("000002", 0.20, 85, "电子"), tgt("000003", 0.15, 80, "电子"),
               tgt("000004", 0.10, 70, "银行"), tgt("000005", 0.10, 60, "地产")]
    lim = limits(capacity={"0.000004": 20_000, "0.000005": 50_000})        # 2% 与 5% 的流动性容量
    plan = risk.build(book, RC, lim, [], [], targets, "2026-10-08", "attack")
    got = {s["code"]: s for s in plan.suggestions}
    assert got["000001"]["target_weight"] == pytest.approx(0.15)         # 单票上限
    assert got["000002"]["target_weight"] == pytest.approx(0.15)         # 行业已用 15%，再给 15% 到 30%
    assert "000003" not in got                                            # 行业上限用完
    assert "000004" not in got and any("000004" in n and "流动性容量" in n for n in plan.notes)   # 2% < 最小仓位 3%
    assert got["000005"]["target_weight"] == pytest.approx(0.05)
    assert all(s["shares"] % 100 == 0 for s in plan.suggestions)


def test_cross_fund_cap_by_issuer():
    book = risk.Book(fund=fund_row(currency="HKD", market="HK", style="value"), cash=1e6, positions={}, prices={})
    t = tgt("00001", 0.15, org="ORG1")
    # 全部基金合计 400 万人民币，同一发行人在其他基金已有 30 万：上限 40 万，只剩 10 万人民币 ≈ 10.9 万港元 ≈ 10.9%
    lim = limits(other_funds={"org:ORG1": 300_000}, total_cny=4e6, fx_cny=0.92)
    plan = risk.build(book, RC, lim, [], [], [t], "d", "attack")
    assert plan.suggestions[0]["target_weight"] == pytest.approx(100_000 / 0.92 / 1e6, abs=0.002)
    lim.other_funds = {"org:ORG1": 390_000}
    assert risk.build(book, RC, lim, [], [], [t], "d", "attack").suggestions == []


def test_exposure_cap_trims_and_drift_tolerance():
    positions = {f"0.00000{i}": pos(f"00000{i}", 10_000, sector=f"s{i}") for i in range(1, 6)}   # 5 × 10% = 50%
    book = risk.Book(fund=fund_row(), cash=500_000, positions=positions, prices={s: 10.0 for s in positions})
    plan = risk.build(book, RC, limits(regime_cap=0.3), [], [], [], "d", "defend")        # 趋势基金防守：30%
    trims = [s for s in plan.suggestions if s["action"] == "trim"]
    assert len(trims) == 5 and all(s["shares"] == 4000 and s["priority"] == 3 for s in trims)
    assert plan.cap == 0.3
    # 中性 60%：现仓位 50% 在上限内，不动
    assert not [s for s in risk.build(book, RC, limits(), [], [], [], "d", "neutral").suggestions if s["action"] == "trim"]
    # 二档熔断：上限 50%，现仓位 50%，不降；不开新仓
    plan = risk.build(book, RC, limits(breaker=2, new_entries=0), [], [], [tgt("000009", 0.1)], "d", "attack")
    assert not [s for s in plan.suggestions if s["action"] in ("trim", "buy")]


def test_breaker_one_halves_new_size_and_regime_quota():
    book = risk.Book(fund=fund_row(), cash=1e6, positions={}, prices={})
    full = risk.build(book, RC, limits(), [], [], [tgt("000001", 0.10)], "d", "attack").suggestions[0]
    half = risk.build(book, RC, limits(breaker=1), [], [], [tgt("000001", 0.10)], "d", "attack").suggestions[0]
    assert full["shares"] == 10_000 and half["shares"] == 5_000
    quota = risk.new_entry_quota(fund_row(), RC, "neutral", 0, "paper")
    plan = risk.build(book, RC, limits(new_entries=quota), [], [], [tgt("000001", 0.1), tgt("000002", 0.1, sector="x")], "d", "neutral")
    assert [s["code"] for s in plan.suggestions if s["action"] == "buy"] == ["000001"]
    assert risk.new_entry_quota(fund_row(), RC, "defend", 0, "paper") == 0
    assert risk.new_entry_quota(fund_row(style="value"), RC, "defend", 0, "paper") is None
    assert risk.new_entry_quota(fund_row(style="value"), RC, "attack", 2, "paper") == 0


def test_replacement_buffer_and_sells_before_buys():
    positions = {f"0.00000{i}": pos(f"00000{i}", 10_000, sector=f"s{i}", score=50 + i) for i in range(1, 4)}
    book = risk.Book(fund=fund_row(max_holdings=3), cash=700_000, positions=positions, prices={s: 10.0 for s in positions})
    weak = tgt("000009", 0.1, score=55, sector="z")           # 只比最弱的 51 分高 8%：不换
    strong = tgt("000008", 0.1, score=70, sector="y")         # 高 37%：换掉最弱的 000001
    plan = risk.build(book, RC, limits(), [], [], [strong, weak], "d", "attack")
    acts = [(s["action"], s["code"]) for s in plan.suggestions]
    assert ("sell", "000001") in acts and ("buy", "000008") in acts and ("buy", "000009") not in acts
    assert acts.index(("sell", "000001")) < acts.index(("buy", "000008"))
    # 非调仓日不替换；条件单（可能不触发）也不替换
    plan = risk.build(book, RC, limits(rebalance_day=False), [], [], [strong], "d", "attack")
    assert not [s for s in plan.suggestions if s["action"] == "buy"]
    brk = tgt("000008", 0.1, score=70, sector="y", entry={"type": "breakout", "level": 10.5, "limit": 10.8})
    assert not risk.build(book, RC, limits(), [], [], [brk], "d", "attack").suggestions


def test_exit_signals_lots_and_stop_orders():
    positions = {"0.000001": pos("000001", 1000, stop=9.0), "0.000002": pos("000002", 150, stop=9.5),
                 "0.000003": pos("000003", 1000, stop=8.0)}
    book = risk.Book(fund=fund_row(), cash=1e6, positions=positions, prices={s: 10.0 for s in positions})
    exits = [(positions["0.000001"], ExitSignal("sell", 1.0, 1, "跌破止损")),
             (positions["0.000002"], ExitSignal("trim", 0.5, 3, "跌破 50 日线，减半"))]
    plan = risk.build(book, RC, limits(), exits, [], [], "d", "attack")
    by = {(s["action"], s["code"]): s for s in plan.suggestions}
    assert by[("sell", "000001")]["shares"] == 1000 and by[("sell", "000001")]["kind"] == "open"
    assert ("trim", "000002") not in by and any("不足一手" in n for n in plan.notes)   # 150 股减半不足一手
    assert by[("stop", "000003")]["trigger"] == {"type": "stop", "price": 8.0}         # 没有卖出计划的持仓挂止损单
    assert ("stop", "000001") not in by


# ---------------------------------------------------------------- 账本


def test_ledger_fills_fees_and_limits():
    a = fcfg.DEFAULT_COSTS["A"]
    st = ledger.State(fund={"id": "f", "market": "A", "initial_capital": 1e6}, cash=1e6, positions={})
    day = "2026-10-08"
    buy = {"id": 1, "secid": "0.000001", "code": "000001", "name": "甲", "action": "buy", "kind": "open", "shares": 1000,
           "limit_price": 10.5, "stop": 9.0, "reason": "测试", "priority": 6, "trigger": {"full_weight": 0.1, "score": 80}}
    bar = {"date": day, "open": 10.0, "high": 10.4, "low": 9.9, "close": 10.2, "volume": 1e6}
    fills = ledger.settle(st, [buy], {"0.000001": bar}, {"0.000001": 9.8}, {}, a, day, {})
    assert fills[0].status == "filled" and fills[0].price == pytest.approx(10.005)
    fee = costs.fees(a, "buy", 1000 * 10.005)
    assert st.cash == pytest.approx(1e6 - 10_005 - fee)
    p = st.positions["0.000001"]
    assert p["avg_cost"] == pytest.approx((10_005 + fee) / 1000) and p["stop"] == 9.0 and p["target_weight"] == 0.1
    # 卖出：已实现盈亏按含费成本
    sell = {"id": 2, "secid": "0.000001", "code": "000001", "name": "甲", "action": "sell", "kind": "open", "shares": 1000,
            "reason": "止盈", "priority": 4}
    bar2 = {**bar, "open": 11.0}
    f2 = ledger.settle(st, [sell], {"0.000001": bar2}, {"0.000001": 10.2}, {}, a, day, {})[0]
    proceeds = 1000 * 10.9945
    assert f2.realized == pytest.approx(proceeds - costs.fees(a, "sell", proceeds) - 1000 * p["avg_cost"])
    assert "0.000001" not in st.positions
    # 一字涨停买不到、开盘高于限价不成交
    lim_bar = {"date": day, "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "volume": 1e5}
    assert ledger.try_fill(buy, lim_bar, day, "A", 10.0, None)[0] == "unfilled"
    assert ledger.try_fill({**buy, "limit_price": 10.2}, {**bar, "open": 10.3}, day, "A", 10.0, None)[0] == "unfilled"
    # 停牌
    assert ledger.try_fill(buy, None, day, "A", 10.0, None)[0] == "unfilled"


def test_ledger_conditional_breakout_and_stop():
    day = "2026-10-08"
    brk = {"action": "buy", "kind": "conditional", "limit_price": 10.6, "trigger": {"type": "breakout", "level": 10.3, "volume_ratio": 1.5}}
    bar = {"date": day, "open": 10.1, "high": 10.8, "low": 10.0, "close": 10.7, "volume": 2e6}
    assert ledger.try_fill(brk, bar, day, "A", 10.0, 1e6) == ("filled", 10.3, "突破 10.300 成交")
    assert ledger.try_fill(brk, {**bar, "open": 10.4}, day, "A", 10.0, 1e6)[1] == 10.4          # 高开在限价内按开盘
    assert ledger.try_fill(brk, {**bar, "open": 10.7}, day, "A", 10.0, 1e6)[0] == "expired"      # 高开超过限价不追
    assert ledger.try_fill(brk, {**bar, "volume": 1e6}, day, "A", 10.0, 1e6)[0] == "expired"     # 量能不足
    assert ledger.try_fill(brk, {**bar, "high": 10.2}, day, "A", 10.0, 1e6)[0] == "expired"      # 没突破
    stop = {"action": "stop", "kind": "conditional", "trigger": {"type": "stop", "price": 9.5}}
    assert ledger.try_fill(stop, {**bar, "low": 9.4}, day, "A", 10.0, None)[1] == 9.5
    assert ledger.try_fill(stop, {**bar, "open": 9.2, "low": 9.0}, day, "A", 10.0, None)[1] == 9.2   # 跳空低开按开盘
    assert ledger.try_fill(stop, bar, day, "A", 10.0, None)[0] == "expired"


def test_nav_and_stats():
    st = ledger.State(fund={"id": "f", "initial_capital": 1e6}, cash=500_000, positions={"x": {"shares": 1000, "avg_cost": 400.0}})
    row = ledger.nav_row(st, {"x": 450.0}, "2026-10-08", {"peak": 1.02, "nav": 1.0}, 3100.0, 3000.0)
    assert row["equity"] == 950_000 and row["nav"] == 0.95 and row["peak"] == 1.02
    assert row["drawdown"] == pytest.approx(1 - 0.95 / 1.02) and row["exposure"] == pytest.approx(450 / 950)
    assert row["benchmark_nav"] == pytest.approx(3100 / 3000)
    s = ledger.trade_stats([{"side": "sell", "realized": 300.0}, {"side": "sell", "realized": -100.0},
                            {"side": "sell", "realized": 100.0}, {"side": "buy", "realized": None}])
    assert s["n"] == 3 and s["win_rate"] == pytest.approx(2 / 3) and s["payoff"] == pytest.approx(2.0)
    assert s["expectancy"] == pytest.approx(2 / 3 * 200 - 1 / 3 * 100)
    assert ledger.max_drawdown([1.0, 1.1, 0.99, 1.2, 1.08]) == pytest.approx(0.1)


# ---------------------------------------------------------------- 策略（合成日线）


def trend_ctx(app, as_of: date, n_strong: int = 3) -> tuple[Context, dict]:
    end = calendar.prev_trading_day("A", as_of)
    days = trading_days("A", end, 300)
    rows, ind = [], {}
    specs = [(f"60000{i}", 0.006 - 0.0005 * i, True) for i in range(n_strong)]                  # 强势、最后一天放量突破
    specs += [("600100", 0.006, False)]                                                          # 强势但还没突破
    specs += [(f"00000{i}", -0.0005 * (i + 1), True) for i in range(1, 9)]                       # 弱势
    for code, g, brk in specs:
        r = a_row(code, f"股{code}", f"行业{code[-1]}")
        rows.append(r)
        c = trend_closes(300, g, breakout=1.02 if brk else None) if g > 0 else 20 * (1 + g) ** np.arange(300)
        v = np.full(300, 1e6)
        if brk:
            v[-1] = 3e6
        b = data.pd.DataFrame(bar_rows(days, c, v))
        ind[r["secid"]] = data.indicators(b, "A")
    ctx = Context(app=app, market="A", as_of=as_of, expected=end, universe=rows, ind=ind, min_turnover=1e6, participation=0.1)
    return ctx, {r["code"]: r["secid"] for r in rows}


def test_trend_strategy_selects_breakouts_and_sizes_by_risk(app):
    as_of = date(2026, 10, 8)
    ctx, sid = trend_ctx(app, as_of)
    s = ASectorTrend({"rs_top": 0.35})
    out = s.score(ctx)
    codes = [t.code for t in out]
    assert "600000" in codes and "600100" in codes and not any(c.startswith("0000") for c in codes)
    t = next(t for t in out if t.code == "600000")
    assert t.entry["type"] == "open" and t.tranches == 2                    # 昨天已放量突破
    w = next(t for t in out if t.code == "600100")
    assert w.entry["type"] == "breakout" and w.entry["level"] == pytest.approx(ctx.ind[sid["600100"]]["pivot"], abs=0.01)
    d = 1 - t.stop / t.entry["level"]
    assert 0.03 <= d <= 0.075 and t.weight == pytest.approx(min(0.01 / d, 0.15), abs=1e-3)
    assert any("未使用行业动量" in n for n in ctx.notes)                   # 每个行业只有一只


def test_trend_exits_trailing_and_adds(app):
    as_of = date(2026, 10, 8)
    ctx, sid = trend_ctx(app, as_of)
    s = ASectorTrend()
    ind = ctx.ind[sid["600000"]]
    p = {"secid": sid["600000"], "code": "600000", "name": "股600000", "shares": 1000, "avg_cost": ind["close"] * 0.8,
         "entry_price": ind["close"] * 0.8, "opened_on": "2026-09-01", "stop": ind["close"] * 0.7, "high_close": 0.0,
         "tranches": 1, "flags": {}}
    u = s.update(p, ctx)                                                     # 浮盈 25%：移动止损上移，不低于成本
    assert u["high_close"] == ind["close"] and u["stop"] >= p["entry_price"] and u["stop"] > p["stop"]
    assert s.check_exit({**p, **u}, ctx) is None
    assert s.check_exit({**p, "stop": ind["close"] * 1.01}, ctx).priority == 1      # 收盘跌破止损
    tp = {**p, "entry_price": ind["close"] * 1.05, "avg_cost": ind["close"] * 1.05, "opened_on": "2026-08-03"}
    sig = s.check_exit(tp, ctx)
    assert sig.action == "sell" and "时间止损" in sig.reason
    assert s.add_signal({**p, "opened_on": "2026-09-01"}, ctx) is not None          # 站稳且浮盈：买第二批
    assert s.add_signal({**p, "opened_on": calendar.prev_trading_day("A", ctx.expected).isoformat()}, ctx) is None
    weak = ctx.ind[sid["000001"]]
    wp = {**p, "secid": sid["000001"], "code": "000001", "entry_price": weak["close"] * 0.5, "stop": weak["close"] * 0.4,
          "opened_on": ctx.expected.isoformat()}
    assert s.check_exit(wp, ctx).action == "trim"                                  # 跌破 50 日线减半
    assert s.check_exit({**wp, "flags": {"half_off": "x"}}, ctx) is None


def hk_ctx(app, as_of: date, trackers=None, kill=None) -> Context:
    end = calendar.prev_trading_day("HK", as_of)
    days = trading_days("HK", end, 60)
    rows = [hk_row("00001", "优质便宜", 25, 6), hk_row("00002", "优质偏贵", 30, 40), hk_row("00003", "低 ROE", 5, 4),
            hk_row("00004", "负 FCF", 20, 5, ocf=-10e8), hk_row("00005", "一般便宜", 14, 7)]
    rows += [hk_row(f"0010{i}", f"填充{i}", 13 + i, 15 + 5 * i) for i in range(6)]
    ind = {r["secid"]: data.indicators(data.pd.DataFrame(bar_rows(days, np.full(60, 20.0), np.full(60, 1e6), "HK")), "HK")
           for r in rows}
    return Context(app=app, market="HK", as_of=as_of, expected=end, universe=rows, ind=ind, min_turnover=1e6, participation=0.1,
                   rebalance_day=True, trackers=trackers or {}, kill=kill or {}, hkdcny=0.92)


def test_hk_value_strategy_ranks_quality_and_cheapness(app):
    as_of = date(2026, 10, 8)
    s = HKQualityValue({"cheap_quantile": 0.4})
    out = s.score(hk_ctx(app, as_of))
    codes = [t.code for t in out]
    assert codes[0] == "00001" and "00003" not in codes and "00004" not in codes and "00002" not in codes
    assert out[0].weight == pytest.approx(0.15) and all(0.05 <= t.weight <= 0.15 for t in out) and out[0].tranches == 3
    # 有目标价：现价 / 目标价 > 0.75 时即使便宜也不买；≤ 0.75 时偏贵的也可以买
    tr = {"116.00001": {"ratio": 0.9, "target": 22.0, "price_equiv_target": 22.2},
          "116.00002": {"ratio": 0.6, "target": 33.0, "price_equiv_target": 33.3}}
    codes = [t.code for t in s.score(hk_ctx(app, as_of, trackers=tr))]
    assert "00001" not in codes and "00002" in codes
    # 证伪条件已触发的不买，持有的清仓
    kill = {"116.00001": [{"status": "triggered", "falsifies": True, "action": "exit", "description": "ROE 连续两季下滑"}]}
    ctx = hk_ctx(app, as_of, kill=kill)
    assert "00001" not in [t.code for t in s.score(ctx)]
    p = {"secid": "116.00001", "code": "00001", "name": "优质便宜", "shares": 1000, "avg_cost": 20.0, "tranches": 1, "flags": {}}
    assert s.check_exit(p, ctx).priority == 2


def test_hk_value_exits_review_and_tranches(app):
    as_of = date(2026, 10, 8)
    s = HKQualityValue()
    ctx = hk_ctx(app, as_of, trackers={"116.00001": {"ratio": 0.5, "target": 40.0, "price_equiv_target": 15.0}})
    p = {"secid": "116.00001", "code": "00001", "name": "优质便宜", "shares": 1000, "avg_cost": 12.0, "tranches": 1, "flags": {}}
    assert s.check_exit(p, ctx).priority == 4                         # 现价 20 ≥ 目标价 15 × 1.2：止盈
    ctx = hk_ctx(app, as_of)
    p = {**p, "avg_cost": 26.0}                                        # 跌 23%：复核提醒，不交易
    sig = s.check_exit(p, ctx)
    assert sig.action == "review" and sig.kind == "note"
    assert s.check_exit({**p, "flags": {"review": "x"}}, ctx) is None
    assert s.add_signal({**p, "avg_cost": 21.5, "target_weight": 0.12}, ctx) is not None     # 回调 7%：补下一批
    assert s.add_signal({**p, "avg_cost": 19.0, "tranches": 3}, ctx) is None
    assert s.check_exit({**p, "secid": "116.00003", "code": "00003", "avg_cost": 20.0}, ctx).reason.startswith("调仓日复查")


# ---------------------------------------------------------------- 第一阶段、日线刷新


def test_stage1_exclusions_and_rank(app):
    cfg = fcfg.FundsFile.model_validate({"universe": {"min_mcap": {"HK": 5e9, "A": 5e9}, "bars_universe": {"HK": 2, "A": 2}}})
    rows = [a_row("600001", "大", "x", mcap=9e10), a_row("600002", "中", "x", mcap=5e10), a_row("600003", "小", "x", mcap=1e10),
            a_row("600004", "*ST差", "x"), a_row("600005", "退市某", "x"), a_row("600006", "微", "x", mcap=1e9)]
    seed_universe(app, rows)
    out, dropped = data.stage1(app, cfg, "A")
    assert [u["code"] for u in out] == ["600001", "600002"]
    assert dropped == {"ST": 1, "退市整理": 1, "市值低于下限": 1, "市值排名在前 N 之外": 1}


def test_refresh_bars_incremental_rebase_and_abort(app, monkeypatch):
    from stockagent import app as appmod

    cfg = fcfg.FundsFile.model_validate({"bars": {"history": 30, "incremental": 5, "max_errors": 2}})
    days = trading_days("A", calendar.prev_trading_day("A", today_local()), 30)
    calls = []

    def fake(session, secid, limit=400):
        calls.append((secid, limit))
        if secid == "0.bad1" or secid == "0.bad2":
            return []
        scale = 0.9 if secid == "0.rebased" and limit == 5 else 1.0
        return bar_rows(days[-limit:], np.linspace(10, 12, 30)[-limit:] * scale)

    monkeypatch.setattr(appmod.eastmoney, "fetch_klines", fake)
    monkeypatch.setattr(appmod.tencent, "fetch_klines", lambda *a, **k: [])
    old = days[:-2]
    for sid in ("0.fresh", "0.rebased"):
        app.db.upsert_bars(sid, bar_rows(old, np.linspace(10, 12, 30)[:-2]))
    sleeps = []
    st = data.refresh_bars(app, cfg, ["0.new", "0.fresh", "0.rebased"], sleep=sleeps.append)
    by = {}
    for s, n in calls:
        by.setdefault(s, []).append(n)
    assert by["0.new"] == [30] and by["0.fresh"] == [5] and by["0.rebased"] == [5, 30]     # 复权变化后重取全量
    assert st.ok == 3 and st.failed == 0 and st.full == 2
    calls.clear()
    st = data.refresh_bars(app, cfg, ["0.bad1", "0.bad2", "0.later"], sleep=sleeps.append)
    assert st.aborted and st.failed == 2 and "0.later" not in [c[0] for c in calls] and sleeps   # 连续失败停止并退避


# ---------------------------------------------------------------- 端到端（合成股票池）


@pytest.fixture
def funded(app, project):
    (project / "funds.yaml").write_text(textwrap.dedent(FUNDS_YAML), encoding="utf-8")
    as_of = as_of_date()
    end = calendar.prev_trading_day("A", as_of)
    days = trading_days("A", end, 300)
    rows = []
    # 4 只强势放量突破；8 只缓慢上涨（宽度高、市场状态为进攻，但相对强度排不进前 30%）
    specs = [(f"60000{i}", 0.006 - 0.0004 * i, True) for i in range(4)] + [(f"00000{i}", 0.0002 * i, False) for i in range(1, 9)]
    for code, g, brk in specs:
        r = a_row(code, f"股{code}", f"行业{code[-1]}")
        rows.append(r)
        c = trend_closes(300, g, breakout=1.02) if brk else 20 * (1 + g) ** np.arange(300)
        v = np.full(300, 1e6)
        v[-1] = 3e6
        app.db.upsert_bars(r["secid"], bar_rows(days, c, v))
    hdays = trading_days("HK", calendar.prev_trading_day("HK", as_of), 300)
    hk = [hk_row("00001", "优质便宜", 25, 6), hk_row("00005", "一般便宜", 14, 7)] + [hk_row(f"0010{i}", f"填充{i}", 13 + i, 15 + 5 * i) for i in range(6)]
    for r in hk:
        app.db.upsert_bars(r["secid"], bar_rows(hdays, np.full(300, 20.0), np.full(300, 1e6), "HK"))
    rows += hk
    seed_universe(app, rows)
    app.db.upsert_bars("1.000300", bar_rows(days, noisy_index(300)))
    app.db.upsert_bars("100.HSI", bar_rows(hdays, noisy_index(300, seed=3) * 8, market="HK"))
    cfg = fcfg.load(app)
    store.sync(app.db, cfg.funds, as_of.isoformat())
    return cfg, as_of


def add_day_bar(app, secid: str, day: date, o: float, h: float, lo: float, c: float, v: float = 3e6) -> None:
    app.db.upsert_bars(secid, [{"date": day.isoformat(), "open": o, "high": h, "low": lo, "close": c, "volume": v, "amount": v * c * 100,
                                "turnover": None}])


def test_end_to_end_plan_close_alert(funded, app):
    cfg, as_of = funded
    n0 = len(store.suggestions(app.db))
    plans, msgs = engine.plan(app, cfg, as_of, dry_run=True)
    assert len(store.suggestions(app.db)) == n0                              # dry-run 不写库
    by = {p.fund["id"]: p for p in plans}
    assert by["a_trend"].regime["state"] == "attack"
    a_buys = [s for s in by["a_trend"].suggestions if s["action"] == "buy"]
    assert a_buys and all(s["shares"] % 100 == 0 for s in a_buys)
    hk_buys = [s for s in by["hk_value"].suggestions if s["action"] == "buy"]
    assert hk_buys and hk_buys[0]["code"] == "00001" and all(s["trigger"]["lot_known"] is False for s in hk_buys)
    msgs_out = report.plan_messages(plans, as_of)
    text = "\n".join(t for _, t in msgs_out)
    assert "【模拟基金】盘前计划" in msgs_out[0][0] and report.DISCLAIMER in text and "首批 1/3" in text and "首批 1/2" in text
    assert all(len(t) <= 4000 for _, t in msgs_out)

    plans, _ = engine.plan(app, cfg, as_of)
    saved = store.suggestions(app.db, "a_trend", day=as_of.isoformat(), statuses=("pending",))
    assert {s["code"] for s in saved} >= {s["code"] for s in a_buys}
    assert store.regime(app.db, "A")["state"] == "attack" and store.latest_snapshot(app.db, "a_trend")

    # 当天日线：A 股第一只开盘成交；港股按开盘成交
    first = saved[0]
    add_day_bar(app, first["secid"], as_of, first["limit_price"] * 0.99, first["limit_price"], first["limit_price"] * 0.97,
                first["limit_price"] * 0.995)
    for s in store.suggestions(app.db, "hk_value", day=as_of.isoformat(), statuses=("pending",)):
        add_day_bar(app, s["secid"], as_of, 20.0, 20.3, 19.8, 20.1, 1e6)
    add_day_bar(app, "1.000300", as_of, 3000, 3010, 2990, 3005)
    results, _ = engine.close(app, cfg, as_of, fetch=False)
    rc = {r.fund["id"]: r for r in results}
    a_filled = [x for x in rc["a_trend"].fills if x.status == "filled"]
    assert len(a_filled) == 1 and a_filled[0].suggestion["code"] == first["code"]
    held = store.positions(app.db, "a_trend")
    assert len(held) == 1 and held[0]["stop"] and held[0]["tranches"] == 1
    f = store.fund(app.db, "a_trend")
    t = store.trades(app.db, "a_trend")[0]
    assert f["cash"] == pytest.approx(1e6 - t["amount"] - t["fees"])
    nav = store.last_nav(app.db, "a_trend")
    assert nav["date"] == as_of.isoformat() and nav["positions"] == 1 and 0.99 < nav["nav"] < 1.01
    assert store.positions(app.db, "hk_value") and store.last_nav(app.db, "hk_value")["exposure"] > 0
    assert not store.suggestions(app.db, "a_trend", statuses=("pending",))     # 其余条件单作废
    text = "\n".join(x for _, x in report.close_messages(app, results, as_of))
    assert "模拟成交" in text and "净值" in text

    # 下一个交易日的计划：持仓挂止损单；盘中价格跌破止损时提醒，同一天只提醒一次
    nxt = calendar.next_trading_day("A", as_of)
    plans, _ = engine.plan(app, cfg, nxt)
    stops = store.suggestions(app.db, "a_trend", day=nxt.isoformat(), statuses=("pending",))
    stop = next(s for s in stops if s["action"] == "stop")
    now = datetime(nxt.year, nxt.month, nxt.day, 10, 30, tzinfo=CST)
    quotes = {stop["secid"]: {"price": stop["stop"] * 0.98, "open": stop["stop"], "prev_close": stop["stop"] * 1.01}}
    alerts, _ = engine.alert(app, cfg, now, dry_run=True, quotes=quotes)
    assert [a.kind for a in alerts if a.secid == stop["secid"]] == ["stop"]
    alerts, _ = engine.alert(app, cfg, now, quotes=quotes)
    assert alerts and not engine.alert(app, cfg, now, quotes=quotes)[0]      # 去重
    off, msgs = engine.alert(app, cfg, now.replace(hour=12, minute=0), quotes=quotes)
    assert not off and any("不在交易时段" in m for m in msgs)


def test_close_breaker_and_resume(funded, app, capsys):
    cfg, as_of = funded
    f = store.fund(app.db, "a_trend")
    store.set_fund(app.db, "a_trend", cash=830_000)                          # 现金亏掉 17%，没有持仓
    app.db._exec("INSERT INTO nav_daily (fund_id, date, nav, equity, cash, exposure, peak, drawdown, positions, breaker, created_at) "
                 "VALUES ('a_trend', '2020-01-01', 1.0, 1e6, 1e6, 0, 1.0, 0, 0, 0, 'x')")
    add_day_bar(app, "1.000300", as_of, 3000, 3010, 2990, 3005)
    results, _ = engine.close(app, cfg, as_of, ["a_trend"], fetch=False)
    assert results[0].breaker_after == 3 and store.fund(app.db, "a_trend")["breaker"] == 3
    plans, _ = engine.plan(app, cfg, calendar.next_trading_day("A", as_of), ["a_trend"], dry_run=True)
    assert not [s for s in plans[0].suggestions if s["action"] == "buy"] and plans[0].cap == cfg.risk.halt_exposure
    cli.main(["-c", str(app.root / "config.yaml"), "fund", "resume", "a_trend"])
    assert store.fund(app.db, "a_trend")["breaker"] == 0 and f["initial_capital"] == 1e6


def test_cli_plan_dry_run_never_pushes(funded, app, monkeypatch, capsys):
    _, as_of = funded
    from stockagent import notify

    monkeypatch.setattr(notify, "push", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应推送")))
    cli.main(["-c", str(app.root / "config.yaml"), "fund", "plan", "--date", as_of.isoformat(), "--dry-run", "--push"])
    out = capsys.readouterr().out
    assert "【模拟基金】盘前计划" in out and "没有发送" in out
    assert not store.suggestions(app.db)
    cli.main(["-c", str(app.root / "config.yaml"), "fund", "status"])
    assert "a_trend" in capsys.readouterr().out
    cli.main(["-c", str(app.root / "config.yaml"), "fund", "plan", "--date", "2026-10-03", "--dry-run"])   # 周六
    assert "休市" in capsys.readouterr().out


def test_jobs_registry_for_fund_tasks(app):
    steps, _, _ = jobs.build(app.watchlist, app.settings.llm.profiles, "fund_plan", flags=["--push"])
    assert steps == [["fund", "plan", "--push"]] and jobs.TASKS["fund_plan"].group == "fund"
    assert {jobs.TASKS[k].group for k in ("fund_alert", "fund_close", "fund_refresh", "fund_weekly")} == {"fund"}
    with pytest.raises(jobs.JobError):
        jobs.build(app.watchlist, app.settings.llm.profiles, "fund_plan", ["acme"])
    with pytest.raises(jobs.JobError):
        jobs.build(app.watchlist, app.settings.llm.profiles, "fund_refresh", flags=["--push"])
    assert "模拟基金" in jobs.CATEGORIES


def test_message_split_by_fund():
    blocks = ["基金甲\n" + "甲" * 3000, "基金乙\n" + "乙" * 3000]
    msgs = report.chunks("【模拟基金】盘前计划 10-08", blocks)
    assert len(msgs) == 2 and msgs[0][0].endswith("（1/2）") and all(report.DISCLAIMER in t and len(t) <= 4000 for _, t in msgs)
    assert len(report.chunks("t", ["短"])) == 1


def test_hk_value_rechecks_quality_after_new_results(app):
    s = HKQualityValue()
    ctx = hk_ctx(app, date(2026, 10, 8))
    ctx.rebalance_day = False
    row = ctx.row("116.00003")                                      # ROE 5%，不达标
    p = {"secid": "116.00003", "code": "00003", "name": "低 ROE", "shares": 1000, "avg_cost": 20.0, "tranches": 1, "flags": {}}
    u = s.update(p, ctx)
    assert u["flags"]["period"] == row["info"]["report_date"]          # 第一次只记下报告期
    assert s.check_exit({**p, **u}, ctx) is None                      # 非调仓日、没有新财报：不复查
    old = {**p, "flags": {"period": "2020-12-31"}}
    assert "period" not in s.update(old, ctx).get("flags", {})        # 新财报不达标：保留旧报告期
    sig = s.check_exit(old, ctx)
    assert sig.priority == 2 and "新财报" in sig.reason
    good = {**old, "secid": "116.00001", "code": "00001"}
    assert s.update(good, ctx)["flags"]["period"] == row["info"]["report_date"]   # 新财报仍达标：更新报告期


# ---------------------------------------------------------------- 价值陷阱防护


def with_flows(row: dict, annual: list[float], ttm: float | None = None, cash: float | None = None) -> dict:
    """按给定的年度 FCF（新 → 旧，资本开支为 0）重写报表；ttm 为最新中报推算用的半年数。"""
    y = today_local().year
    flows = [{"date": f"{y}-06-30", "annual": False, "ocf": (ttm if ttm is not None else annual[0]) / 2, "capex": 0.0},
             {"date": f"{y - 1}-06-30", "annual": False, "ocf": annual[0] / 2, "capex": 0.0}]
    flows += [{"date": f"{y - 1 - i}-12-31", "annual": True, "ocf": v, "capex": 0.0} for i, v in enumerate(annual)]
    flows.sort(key=lambda x: x["date"], reverse=True)
    bal = dict(row["fcf"]["balance"])
    if cash is not None:
        bal["cash"] = cash
    return {**row, "fcf": {"flows": flows, "balance": bal}}


def test_hk_value_caps_outliers_and_checks_fcf_consistency(app):
    s = HKQualityValue({"cheap_quantile": 1.0})
    ctx = hk_ctx(app, date(2026, 10, 8))
    extreme = hk_row("00009", "极端", 80, 2)                                    # ROE 80%、盈利收益率 50%
    swing = with_flows(hk_row("00010", "波动", 25, 6), [40e8, 2e8, 30e8])       # 最差一年只有平均的 8%
    short = with_flows(hk_row("00011", "新上市", 25, 6), [40e8])                # 只有一个年度
    cashbox = hk_row("00012", "囤现金", 25, 6, mcap=50e8)
    cashbox["info"] = {**cashbox["info"], "dividend_yield": None, "payout": None}  # 净现金高、不派息
    ctx.universe = ctx.universe + [extreme, swing, short, cashbox]
    for r in (extreme, swing, short, cashbox):
        ctx.ind[r["secid"]] = ctx.ind["116.00001"]
    m, fail = s._metrics(extreme, ctx)
    assert fail is None and m["roe_s"] == 40 and m["ey_s"] == 0.20 and "按上限" in m["outliers"]
    assert "波动" in s._metrics(swing, ctx)[1]
    assert "数据不足" in s._metrics(short, ctx)[1]
    assert s._metrics(cashbox, ctx)[1].startswith("风险标记")                  # 选股器的 no_payout 标记
    out = {t.code: t for t in s.score(ctx)}
    assert "00010" not in out and "00011" not in out and "00012" not in out
    assert "按上限" in out["00009"].reason and out["00009"].metrics["fcf_yield"] <= 0.20


def test_exclusions_from_list_keywords_and_negative_events(app):
    from stockagent.utils import iso, utcnow

    cfg = fcfg.FundsFile.model_validate({"exclusions": {"codes": {"HK:00001": "控股股东清盘"}, "name_keywords": {"恒大": "集团清盘"}}})
    rows = [hk_row("00001", "优质便宜", 25, 6), hk_row("06666", "恒大物业", 30, 6), hk_row("00005", "一般便宜", 14, 7),
            hk_row("00007", "正常", 20, 8)]
    seed_universe(app, rows)
    sc_store.add_events(app.db, [{"uid": "e1", "secid": "116.00005", "market": "HK", "code": "00005", "name": "一般便宜",
                                  "source": "hkex", "kind": "default", "direction": "negative", "weight": 3.0,
                                  "title": "有关清盘呈请的公告", "published_at": iso(utcnow())}])
    from stockagent.fund import guards

    ex, hits = guards.exclusions(app, cfg, rows)
    assert ex["116.00001"].startswith("排除名单") and "恒大" in ex["116.06666"] and "清盘呈请" in ex["116.00005"]
    assert "116.00007" not in ex and hits == {"名单": 1, "名称关键词": 1, "负面公告": 1}
    ctx = hk_ctx(app, date(2026, 10, 8))
    ctx.excluded = {"116.00001": "排除名单：控股股东清盘"}
    s = HKQualityValue({"cheap_quantile": 1.0})
    assert "00001" not in [t.code for t in s.score(ctx)]
    assert ctx.excluded_hits and ctx.excluded_hits[0][0] == "00001"
    p = {"secid": "116.00001", "code": "00001", "name": "优质便宜", "shares": 1000, "avg_cost": 20.0, "tranches": 1, "flags": {}}
    sig = s.check_exit(p, ctx)
    assert sig.action == "sell" and sig.priority == 2 and "风险排除" in sig.reason


def test_llm_veto_replaces_vetoed_buys_caches_and_fails_closed(funded, app, monkeypatch):
    from stockagent.fund import guards

    cfg, as_of = funded
    calls = []

    def fake_judge(app_, cfg_, fund_name, u, metrics, events, flags):
        calls.append(u["code"])
        if u["code"] == "00001":
            return {"verdict": "veto", "reason": "控股股东清盘中（背景知识，可能过时）", "model": "fake"}
        return {"verdict": "pass", "reason": "未见具体风险", "model": "fake"}

    monkeypatch.setattr(guards, "judge", fake_judge)
    store.set_fund(app.db, "hk_value", params_json=json.dumps({"cheap_quantile": 0.6}))   # 候选多几只，便于递补
    plans, _ = engine.plan(app, cfg, as_of, ["hk_value"])
    fp = plans[0]
    buys = [s["code"] for s in fp.suggestions if s["action"] == "buy"]
    assert "00001" not in buys and buys                                   # 被否决的由后面的候选递补
    assert "00001" in calls and any("模型否决 1 只" in n and "00001" in n for n in fp.notes)
    assert json.loads(app.db.kv_get(guards.cache_key("116.00001", today_local().replace(month=6, day=30).isoformat())))["verdict"] == "veto"
    text = "\n".join(t for _, t in report.plan_messages(plans, as_of))
    assert "模型否决" in text
    # 第二次用缓存，不再调用模型
    calls.clear()
    engine.plan(app, cfg, as_of, ["hk_value"], dry_run=True)
    assert calls == []
    # 模型不可用：没有缓存的新建仓一律跳过
    for k in list(app.db.kv_prefix("fund_veto:")):
        app.db.kv_delete(k)
    monkeypatch.setattr(app.llm, "unavailable_reason", lambda task: "没有配置 key")
    plans, _ = engine.plan(app, cfg, as_of, ["hk_value"], dry_run=True)
    assert not [s for s in plans[0].suggestions if s["action"] == "buy"]
    assert any("宁可不买" in n for n in plans[0].notes)
    assert not app.db.kv_prefix("fund_veto:")                              # dry-run 不写缓存
