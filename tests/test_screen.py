"""选股器：批量接口翻页、FCF 口径、事件归类、四个桶的打分与分层、流程、晋级、主题、WebUI（全部离线）。"""

from __future__ import annotations

import textwrap
from datetime import timedelta

import pytest
import yaml

from stockagent import cli, fcf, jobs
from stockagent.screen import buckets, events, promote, quick, store, themes
from stockagent.screen import run as sc_run
from stockagent.sources import emscreen
from stockagent.utils import iso, today_local, utcnow

TODAY = today_local()


def flows(ocf=(60e8, 50e8, 40e8, 30e8), capex=(20e8, 20e8, 20e8, 20e8)):
    """最新中报 + 3 个年报 + 上年中报（TTM 用）。"""
    y = TODAY.year
    return [
        {"date": f"{y}-06-30", "annual": False, "ocf": ocf[0] / 2, "capex": capex[0] / 2},
        {"date": f"{y - 1}-12-31", "annual": True, "ocf": ocf[1], "capex": capex[1]},
        {"date": f"{y - 1}-06-30", "annual": False, "ocf": ocf[1] / 2, "capex": capex[1] / 2},
        {"date": f"{y - 2}-12-31", "annual": True, "ocf": ocf[2], "capex": capex[2]},
        {"date": f"{y - 3}-12-31", "annual": True, "ocf": ocf[3], "capex": capex[3]},
    ]


def balance(cash=100e8, debt=10e8, minority=0.0):
    return {"date": f"{TODAY.year}-06-30", "cash": cash, "cash_items": {"货币资金": cash}, "invest": 0.0, "debt": debt,
            "debt_items": {}, "minority": minority, "liabilities": 50e8, "restricted": 0.0}


def uni(secid="116.00001", market="HK", code="00001", name="现金牛", mcap=100e8, price=10.0, fcf_data=None, **kw):
    info = {"dividend_yield": 0.07, "payout": 0.5, "profit_yoy": 5.0, "revenue_yoy": 3.0, "gross_margin": 30.0,
            "gross_margin_prev": 29.0, "report_date": f"{TODAY.year}-06-30"} | kw.pop("info", {})
    return {"secid": secid, "market": market, "code": code, "name": name, "org_code": kw.pop("org_code", None),
            "industry": kw.pop("industry", "食品"), "org_type": "一般企业", "st": 0, "listing_date": "2010-01-01",
            "price": price, "mcap": mcap, "currency": "HKD" if market == "HK" else "CNY",
            "report_ccy": "HKD" if market == "HK" else "CNY", "pe_ttm": kw.pop("pe_ttm", 8.0), "pb": 1.0,
            "quote_date": TODAY.isoformat(), "info": info,
            "fcf": fcf_data if fcf_data is not None else {"flows": flows(), "balance": balance()}, **kw}


def seed(app, rows):
    for m in ("HK", "A"):
        rs = [r for r in rows if r["market"] == m]
        if rs:
            store.upsert_universe(app.db, rs, m)
    store.set_fcf(app.db, {r["secid"]: r["fcf"] for r in rows if r.get("fcf")})


def event(secid, kind, direction="positive", weight=1.0, days_ago=1, title="标题", code="00001", market="HK"):
    return {"uid": f"{secid}{kind}{days_ago}{title}", "secid": secid, "market": market, "code": code, "name": "x",
            "source": "hkex", "kind": kind, "direction": direction, "weight": weight, "title": title, "category": "",
            "url": "https://www1.hkexnews.hk/a.pdf", "published_at": iso(utcnow() - timedelta(days=days_ago))}


# ---------------------------------------------------------------- 数据层与口径

class Resp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class Pager:
    def __init__(self, rows, size=2):
        self.rows, self.size, self.calls = rows, size, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        p = params["pageNumber"]
        chunk = self.rows[(p - 1) * self.size: p * self.size]
        pages = -(-len(self.rows) // self.size)
        return Resp({"result": {"data": chunk, "pages": pages, "count": len(self.rows)}, "message": "ok"})


def test_rows_all_pages_with_a_stable_sort(monkeypatch):
    monkeypatch.setattr(emscreen, "PAGE", 2)
    s = Pager([{"i": i} for i in range(5)])
    rows = emscreen.rows_all(s, "u", "SECUCODE,-REPORT_DATE", reportName="R")
    assert [r["i"] for r in rows] == [0, 1, 2, 3, 4] and len(s.calls) == 3
    assert s.calls[0]["sortColumns"] == "SECUCODE,REPORT_DATE" and s.calls[0]["sortTypes"] == "1,-1"


def test_fcf_metrics_annual_latest_not_double_counted_and_capex_proxy():
    data = {"flows": [{"date": "2025-12-31", "annual": True, "ocf": 30e8, "capex": 10e8},
                      {"date": "2024-12-31", "annual": True, "ocf": 20e8, "capex": 10e8},
                      {"date": "2023-12-31", "annual": True, "ocf": 10e8, "capex": 10e8}], "balance": balance()}
    m = fcf.metrics(data, 100e8)
    assert [x["label"] for x in m["flows"]] == ["2025 年报", "2024 年报", "2023 年报"]
    assert m["fcf_avg3"] == pytest.approx(10e8) and m["fcf_worst"] == 0
    # 资本开支科目缺失（接近 0）而投资活动大额净流出：按投资活动净流出估算
    data["flows"][0].update(capex=0.0, inv=-25e8)
    m = fcf.metrics(data, 100e8)
    assert m["flows"][0]["fcf"] == pytest.approx(5e8) and m["capex_proxy"] == ["2025 年报"]
    assert fcf.payback_years(-1, 1) == 0 and fcf.payback_years(10, -1) is None and fcf.payback_years(10, 5) == 2


def test_hk_fcf_rows_record_net_investing():
    from stockagent.sources import emdata

    cf = [{"REPORT_DATE": "2025-12-31 00:00:00", "DATE_TYPE_CODE": "001", "STD_ITEM_NAME": n, "AMOUNT": v}
          for n, v in (("经营业务现金净额", 100), ("购建固定资产", -30), ("投资业务现金净额", -40))]
    bs = [{"REPORT_DATE": "2025-12-31 00:00:00", "STD_ITEM_NAME": "现金及等价物", "AMOUNT": 50}]
    d = emdata.hk_fcf_from_rows(cf, bs)
    assert d["flows"][0] == {"date": "2025-12-31", "annual": True, "ocf": 100, "capex": 30, "inv": -40}


def test_event_rules():
    k = lambda t, c="", m="HK": (r.kind, r.direction) if (r := events.classify(t, c, m)) else None
    assert k("翌日披露報表", "公告及通告 - [股份購回]") == ("buyback", "positive")
    assert k("年度業績公告", "[末期業績 / 核數師發出「非標準報告」]") == ("audit", "negative")
    assert k("建議宣派特別股息") == ("special_dividend", "positive")
    assert k("購回股份的一般授權") is None
    assert k("关于回购注销部分限制性股票的公告", m="A") is None
    assert k("关于控股股东增持公司股份计划的公告", m="A") == ("insider_buy", "positive")
    assert k("关于股东减持股份计划的公告", m="A") == ("reduce", "negative")
    assert k("供股", m="A") is None  # 供股只在港股规则里


def test_a_periods():
    from stockagent.screen import universe

    assert universe.a_periods("2026-06-30") == {"cf": ["2026-06-30", "2025-12-31", "2025-06-30", "2024-12-31", "2023-12-31"],
                                                "bs": ["2026-06-30", "2025-12-31"], "ind": ["2026-06-30", "2025-06-30"]}
    assert universe.a_periods("2025-12-31")["cf"] == ["2025-12-31", "2024-12-31", "2023-12-31"]


# ---------------------------------------------------------------- 打分与分层

def ctx(app, evs=None, themes_=None, cal=None):
    return buckets.Ctx(cfg=app.settings.screen, hkdcny=0.9, events=evs or {}, themes=themes_ or {}, calendar=cal or {})


def cal_item(days=30, kind="dividend", confirmed=True, direction="positive", text="已公告派息"):
    return {"date": (TODAY + timedelta(days=days)).isoformat(), "kind": kind, "direction": direction, "confirmed": confirmed, "text": text}


def payer(years=5, items=(), flags=()):
    return {"dividend": {"years": years, "past_ex": [], "upcoming": [], "plans": []}, "items": list(items), "flags": list(flags)}


LIQ = {"median": 5e8, "mean": 5e8, "zero_days": 0, "days": 20, "basis": "成交额", "capacity": 2.5e8}


def test_floor_bucket_odds_and_tiers(app):
    cfg = app.settings.screen
    u = uni(fcf_data={"flows": flows(), "balance": balance(cash=105e8)})  # 市值 100 亿，净现金 95 亿（EV 5 亿），TTM FCF 30 亿
    fm = buckets.floor_metrics(u, cfg, 0.9)
    assert fm["net_cash_ratio"] == pytest.approx(0.95) and fm["ev_small"] and fm["yield_cons"] is None
    assert fm["downside"] == pytest.approx(cfg.min_downside)  # 地板高于现价时下行按下限计
    assert fm["odds"] > cfg.odds_a
    assert buckets.floor_metrics(uni(), cfg, 0.9)["yield_cons"] == pytest.approx(2.0)  # EV 10 亿，保守 FCF 20 亿（3 年平均）
    c = buckets.pool_candidate(buckets.evaluate(u, ctx(app)), "long", cfg)
    assert c["buckets"].startswith("floor") and c["tier"] == "C"  # 没有催化剂、没有派息记录：只是高赔率
    c = buckets.pool_candidate(buckets.evaluate(u, ctx(app, cal={u["secid"]: payer(items=[cal_item()])})), "long", cfg)
    assert c["tier"] == "A" and "已公告派息" in c["catalysts"][0]  # 已公告的派息是催化剂
    far = payer(items=[cal_item(days=200, confirmed=False, text="预计除息")])
    assert buckets.pool_candidate(buckets.evaluate(u, ctx(app, cal={u["secid"]: far})), "event", cfg) is None  # 不在事件池的桶里
    assert buckets.catalysts(buckets.evaluate(u, ctx(app, cal={u["secid"]: far})), "event", cfg) == []  # 超出事件池 90 天窗口
    u2 = uni(info={"dividend_yield": None, "payout": None})
    c2 = buckets.pool_candidate(buckets.evaluate(u2, ctx(app)), "long", cfg)
    assert c2["tier"] == "C" and any(f["code"] == "no_payout" for f in c2["flags"])
    # 以人民币报账的港股：市值按汇率换算
    u3 = uni(report_ccy="CNY")
    assert buckets.floor_metrics(u3, cfg, 0.9)["net_cash_ratio"] == pytest.approx(90e8 / 90e8)
    assert buckets.floor_metrics(uni(report_ccy="CNY"), cfg, None) is None


def test_trap_flags_and_hard_filter(app):
    cfg = app.settings.screen
    bad = uni(fcf_data={"flows": flows(ocf=(-10e8, 10e8, -5e8, 40e8)), "balance": balance(cash=60e8, debt=50e8)})
    fm = buckets.floor_metrics(bad, cfg, 1.0)
    codes = {f["code"] for f in fm["flags"]}
    assert {"fcf_unstable", "fcf_negative", "cash_and_debt"} <= codes
    assert buckets.hard_filter(uni(mcap=1e8), cfg) == "市值低于下限"
    assert buckets.hard_filter(uni(st=1), cfg) == "ST"
    assert buckets.hard_filter(uni(listing_date=(TODAY - timedelta(days=30)).isoformat()), cfg) == "上市不满规定天数"
    assert not buckets.fcf_applicable(uni(industry="银行"), cfg)


def test_event_pool_buyback_cap_and_veto(app):
    cfg = app.settings.screen
    sid = "116.00001"
    daily = [event(sid, "buyback", days_ago=d) for d in range(1, 15)]
    es = buckets.event_summary(daily)
    assert es["strength"] == pytest.approx(1.5)  # 天天回购最多记 1.5
    evs = daily + [event(sid, "special_dividend", weight=2.0)]
    c = buckets.pool_candidate(buckets.evaluate(uni(), ctx(app, {sid: evs})), "event", cfg)
    assert c["buckets"].startswith("event") and c["tier"] == "A"
    veto = evs + [event(sid, "audit", "negative", 3.0, title="核数师辞任")]
    ev = buckets.evaluate(uni(), ctx(app, {sid: veto}))
    assert "event" not in ev["buckets"]
    long = buckets.pool_candidate(ev, "long", cfg)
    assert long["tier"] == "D" and any(f["severity"] == "high" and "核数师" in f["text"] for f in long["flags"])


def test_revision_and_theme_buckets(app):
    cfg = app.settings.screen
    grow = uni(secid="0.300001", market="A", code="300001", pe_ttm=30.0, fcf_data={"flows": flows(ocf=(1e8,) * 4, capex=(2e8,) * 4),
               "balance": balance(cash=5e8, debt=20e8)}, info={"profit_yoy": 80.0, "revenue_yoy": 40.0, "gross_margin": 40.0,
               "gross_margin_prev": 35.0, "dividend_yield": None})
    ev = buckets.evaluate(grow, ctx(app, themes_={"0.300001": [{"theme": "AI", "theme_id": "ai", "node": "光芯片", "bottleneck": True}]}))
    assert "revision" in ev["buckets"] and "theme" in ev["buckets"] and "floor" not in ev["buckets"]
    c = buckets.pool_candidate(ev, "long", cfg)
    assert c["buckets"] == "theme" and c["odds"] is None and c["tier"] == "C"  # 成长型：赔率留给深度报告
    assert any(f["code"] == "growth" for f in c["flags"])
    fc = [event("0.300001", "forecast_down", "negative", 1.5, code="300001", market="A")]
    assert "revision" not in buckets.evaluate(grow, ctx(app, {"0.300001": fc}))["buckets"]


def test_dedupe_ah():
    a = {"secid": "1.600000", "market": "A", "code": "600000", "score": 80, "org_code": "X", "flags": []}
    h = {"secid": "116.00001", "market": "HK", "code": "00001", "score": 90, "org_code": "X", "flags": []}
    out = buckets.dedupe_ah([a, h])
    assert out == [h] and "另有A 股 600000" in h["flags"][0]["text"]


def test_quality_bucket_needs_stable_fcf_and_dividend_record(app):
    cfg = app.settings.screen
    # 市值 200 亿、净现金 90 亿：保守 FCF Yield 20/110 ≈ 18%，股息率 7%（FCF 20 亿 / 市值 = 10% 覆盖）
    u = uni(mcap=200e8)
    ev = buckets.evaluate(u, ctx(app, cal={u["secid"]: payer(years=5)}))
    assert "quality" in ev["buckets"] and all(ev["quality"]["checks"].values())
    c = buckets.pool_candidate(ev, "long", cfg)
    assert c["tier"] == "B"  # 高胜率：稳定现金牛，赔率一般也可以
    assert not buckets.evaluate(u, ctx(app, cal={u["secid"]: payer(years=2)}))["quality"]["checks"]["dividend_years"]
    lumpy = uni(mcap=200e8, fcf_data={"flows": flows(ocf=(60e8, 90e8, 21e8, 40e8)), "balance": balance()})
    q = buckets.evaluate(lumpy, ctx(app, cal={lumpy["secid"]: payer()}))["quality"]
    assert not q["checks"]["fcf_stable"]  # 最差年度 FCF 1 亿，远低于 3 年平均


@pytest.mark.parametrize("args,tier", [
    ((5.0, 0.15, ["x"], False, False, False, 0, True), "A"),
    ((5.0, 0.4, ["x"], False, False, False, 0, True), "C"),     # 下行太大：只剩高赔率
    ((1.5, 0.15, ["x"], False, False, False, 0, True), "B"),    # 有催化剂 + 有地板，赔率一般：高胜率
    ((1.5, 0.4, [], True, False, False, 0, True), "B"),         # 质量红利：高胜率
    ((0.5, 0.15, ["x"], True, False, False, 0, True), "D"),     # 赔率 < 1：不值得
    ((5.0, 0.15, ["x"], False, True, False, 0, True), "C"),     # 有严重风险标记：不算高胜率
    ((5.0, 0.15, ["x"], False, False, True, 0, True), "D"),     # 否决事件
    ((None, None, [], False, False, False, 60, True), "C"),    # 成长型主题
])
def test_tier_rules(app, args, tier):
    odds, down, cats, quality, high, veto, theme, safe = args
    assert buckets.tier_for("long", odds, down, cats, quality, high, veto, theme, safe, app.settings.screen) == tier


def test_calendar_dividends_and_predictions():
    from stockagent.screen import calendar

    today = TODAY
    ly = today.replace(year=today.year - 1)
    hk = [{"ASSIGN_PERIOD": f"{today.year - k}末期", "EX_DIVIDEND_DATE": (ly + timedelta(days=30)).replace(year=today.year - k).isoformat(),
           "IS_DIVIDEND": "1", "PLAN_EXPLAIN": "每股派港币0.5元"} for k in range(1, 5)]
    d = calendar.dividends_hk(hk, today)
    assert d["years"] == 4 and d["upcoming"] == []
    a = [{"REPORT_DATE": f"{today.year - k}-12-31 00:00:00", "PRETAX_BONUS_RMB": 5.0,
          "EX_DIVIDEND_DATE": (today - timedelta(days=100 + 365 * (k - 1))).isoformat(), "ASSIGN_PROGRESS": "实施分配"} for k in range(1, 4)]
    a.append({"REPORT_DATE": f"{today.year}-06-30 00:00:00", "PRETAX_BONUS_RMB": 2.0, "EX_DIVIDEND_DATE": None,
              "ASSIGN_PROGRESS": "董事会预案", "IMPL_PLAN_PROFILE": "10派2元"})
    d = calendar.dividends_a(a, 10.0, today)
    assert d["years"] == 3 and d["dividend_yield"] == pytest.approx(0.05) and "分配预案" in d["plans"][0]["text"]


def test_calendar_build_from_cache(app):
    from stockagent.screen import calendar

    rows = [uni(), uni(secid="1.600001", market="A", code="600001", name="甲公司", price=10.0)]
    seed(app, rows)
    past = TODAY - timedelta(days=330)
    store.bulk_set(app.db, "HK:div", [{"SECUCODE": "00001.HK", "ASSIGN_PERIOD": f"{y}末期", "IS_DIVIDEND": "1", "PLAN_EXPLAIN": "派息",
                                       "EX_DIVIDEND_DATE": past.replace(year=past.year - k).isoformat()}
                                      for k, y in enumerate(range(TODAY.year - 1, TODAY.year - 5, -1))])
    store.bulk_set(app.db, "A:lift", [{"SECURITY_CODE": "600001", "FREE_DATE": (TODAY + timedelta(days=20)).isoformat(),
                                       "FREE_SHARES_TYPE": "首发原股东限售股份", "TOTAL_RATIO": 0.15, "FREE_RATIO": 0.4}])
    cal = calendar.build(app, store.universe(app.db))
    hk = cal["116.00001"]
    assert any(x["kind"] == "dividend" and not x["confirmed"] for x in hk["items"])  # 按去年同期推算
    assert cal["1.600001"]["flags"][0]["severity"] == "high" and "解禁" in cal["1.600001"]["flags"][0]["text"]


def test_liquidity_median_and_suspension(app, monkeypatch):
    import pandas as pd

    from stockagent import tools

    amounts = [1e8] * 18 + [50e8, 50e8]
    df = pd.DataFrame({"date": [f"2026-09-{i + 1:02d}" for i in range(20)], "amount": amounts, "turnover": [None] * 20,
                       "volume": [0, 0, 0] + [1e6] * 17, "close": [1.0] * 20})
    monkeypatch.setattr(tools, "ensure_bars", lambda app, secid, max_age_days=4: df)
    liq = sc_run.liquidity(app, {"secid": "116.00001", "metrics": {"mcap": 1e10}})
    assert liq["median"] == pytest.approx(1e8) and liq["mean"] > 5e8 and liq["zero_days"] == 3
    assert liq["capacity"] == pytest.approx(1e8 * 0.1 * 5)
    a = df.assign(amount=None, volume=[2e5] * 20, close=[5.0] * 20)  # 腾讯 A 股：成交量单位是手
    monkeypatch.setattr(tools, "ensure_bars", lambda app, secid, max_age_days=4: a)
    liq = sc_run.liquidity(app, {"secid": "1.600001", "metrics": {"mcap": 1e10}})
    assert liq["median"] == pytest.approx(2e5 * 100 * 5.0) and liq["basis"] == "成交量 × 收盘价估算"


# ---------------------------------------------------------------- 流程、快筛、晋级

@pytest.fixture
def seeded(app, monkeypatch):
    rows = [uni(), uni(secid="116.00002", code="00002", name="负债王", fcf_data={"flows": flows(), "balance": balance(cash=1e8, debt=300e8)}),
            uni(secid="1.600001", market="A", code="600001", name="甲公司", org_code="ORG1")]
    seed(app, rows)
    store.add_events(app.db, [event("116.00001", "special_dividend", weight=2.0, title="建议宣派特别股息")])
    monkeypatch.setattr(sc_run, "fx_hkdcny", lambda app: 0.9)
    monkeypatch.setattr(sc_run, "liquidity", lambda app, c: LIQ)
    return app


def test_run_end_to_end_with_mock_quick_screen(seeded):
    app = seeded
    res = sc_run.run(app, refresh=False, progress=lambda m: None)
    assert set(res.runs) == {"long", "event"}
    long = store.hits(app.db, res.runs["long"])
    top = next(h for h in long if h["code"] == "00001")
    assert top["tier"] in ("A", "B", "C", "X") and top["quick"]["verdict"] in quick.VERDICT_LABEL and top["events"]
    assert all(h["code"] != "00002" for h in long)  # 净负债公司进不了现金地板桶
    run = store.runs(app.db, "long", 1)[0]
    assert run["status"] == "done" and run["stats"]["evaluated"] == 3


def test_quick_screen_can_only_downgrade(app):
    c = {"tier": "B", "pool": "long", "flags": [], "metrics": {"floor": {"odds": 9, "downside": 0.15}}}
    quick.apply(c, {"verdict": "deep_dive", "catalyst_in_window": True, "catalysts": [{"event": "可能分红"}]})
    assert c["tier"] == "B"
    quick.apply(c, {"verdict": "reject"})
    assert c["tier"] == "X"


def test_illiquid_candidates_dropped(seeded, monkeypatch):
    monkeypatch.setattr(sc_run, "liquidity", lambda app, c: LIQ | {"median": 1.0})
    res = sc_run.run(seeded, ("long",), use_llm=False, refresh=False, progress=lambda m: None)
    assert store.hits(seeded.db, res.runs["long"]) == [] and store.runs(seeded.db, "long", 1)[0]["stats"]["illiquid"] >= 1


def test_promote_writes_watchlist_with_both_listings(seeded):
    app = seeded
    seed(app, [uni(secid="116.00003", code="00003", name="甲公司", org_code="ORG1"),
               *[r for r in store.universe(app.db, "HK") if r["code"] != "00003"]])
    sc_run.run(app, ("long",), use_llm=False, refresh=False, progress=lambda m: None)
    cid, msg = promote.promote(app, "A:600001")
    assert cid == "a600001" and "加入观察清单" in msg
    wl = yaml.safe_load((app.root / "watchlist.yaml").read_text(encoding="utf-8"))
    entry = next(c for c in wl["companies"] if c["id"] == "a600001")
    assert [(l["market"], l["code"]) for l in entry["listings"]] == [("A", "600001"), ("HK", "00003")]
    assert "由选股器加入" in entry["notes"]
    with pytest.raises(promote.PromoteError, match="找不到"):
        promote.promote(app, "999999")


def test_cli_show_and_jobs(seeded, capsys, project):
    sc_run.run(seeded, use_llm=False, refresh=False, progress=lambda m: None)
    cli.main(["-c", str(project / "config.yaml"), "screen", "show", "--tier", "A,B,C"])
    out = capsys.readouterr().out
    assert "长线池" in out and "现金牛" in out and "研究候选，不是买卖建议" in out
    steps, title, _ = jobs.build(seeded.watchlist, seeded.settings.llm.profiles, "screen_run", flags=["--event-only", "--no-llm"])
    assert steps == [["screen", "run", "--event-only", "--no-llm"]] and title == "选股器"
    with pytest.raises(jobs.JobError):
        jobs.build(seeded.watchlist, seeded.settings.llm.profiles, "screen_run", ["acme"])


# ---------------------------------------------------------------- 主题

THEMES = """
themes:
  - id: ai
    name: AI 光互联
    nodes:
      - name: 光芯片
        bottleneck: true
        tickers: ["600001", "HK:09999"]
"""


def test_themes_membership_agent_and_accept(seeded):
    app = seeded
    (app.root / "themes.yaml").write_text(textwrap.dedent(THEMES), encoding="utf-8")
    mem, missing = themes.memberships(app)
    assert mem["1.600001"][0]["bottleneck"] and missing == ["AI 光互联/光芯片：HK:09999"]
    msgs = themes.suggest(app, progress=lambda m: None)  # mock 模型给出无效代码 → 忽略
    assert "已忽略" in msgs[0]
    store.add_theme_ideas(app.db, [{"theme": "ai", "node": "光模块", "secid": "116.00001", "market": "HK", "code": "00001",
                                    "name": "现金牛", "reason": "份额集中"}])
    idea = store.theme_ideas(app.db)[0]
    assert "已把 现金牛" in themes.accept(app, idea["id"])
    t = themes.load(app)
    assert [n.name for n in t.themes[0].nodes] == ["光芯片", "光模块"] and t.themes[0].nodes[1].tickers == ["HK:00001"]
    assert store.theme_ideas(app.db) == []


# ---------------------------------------------------------------- WebUI

def test_screen_page_and_promote(seeded, project, monkeypatch):
    import requests
    from fastapi.testclient import TestClient

    from stockagent.web.server import create_app

    sc_run.run(seeded, use_llm=False, refresh=False, progress=lambda m: None)
    web = create_app(project / "config.yaml", allowed_hosts=["testserver"], start_background=False)

    def no_network(*args, **kwargs):
        raise AssertionError("页面渲染时不应联网")

    monkeypatch.setattr(requests.Session, "request", no_network)
    client = TestClient(web)
    client.get("/")
    for url in ("/screen", "/screen?pool=event&show=all", "/screen/eval"):
        r = client.get(url)
        assert r.status_code == 200, url
    assert "现金牛" in client.get("/screen?show=ABC").text
    hit = next(h for h in store.hits(seeded.db, store.latest_run(seeded.db, "long")["id"]) if h["code"] == "00001")
    r = client.post(f"/screen/promote/{hit['id']}", data={"csrf_token": client.cookies.get("stock_csrf")}, follow_redirects=False)
    assert r.status_code == 303 and "/jobs/" in r.headers["location"]
    holder = web.state.holder
    assert holder.app.watchlist.get("hk00001") is not None
    assert holder.app.db.jobs(5)[0]["task"] == "onboard"
    assert client.post(f"/screen/promote/{hit['id']}", follow_redirects=False).status_code == 403  # 没有 CSRF


# ---------------------------------------------------------------- 港股范围：全部港股 / 仅港股通

def test_refresh_hk_keeps_all_listed_and_marks_connect(app, monkeypatch):
    from stockagent.screen import universe

    listed = {
        "00001": {"name": "港股通甲", "org_code": "O1", "sh": True, "sz": False, "security_type": "非H股", "board": "主板", "listing_date": "2000-01-01"},
        "08001": {"name": "创业板乙", "org_code": "O2", "sh": False, "sz": False, "security_type": "非H股", "board": "创业板", "listing_date": "2010-01-01"},
    }
    ind = {c: [{"STD_REPORT_DATE": "2026-06-30", "TOTAL_MARKET_CAP": 1e10, "ISSUED_COMMON_SHARES": 1e9}] for c in listed}
    monkeypatch.setattr(emscreen, "hk_listed", lambda s: listed)
    monkeypatch.setattr(emscreen, "hk_profiles", lambda s, codes: {})
    monkeypatch.setattr(emscreen, "hk_indicators", lambda s, codes, since: ind)
    monkeypatch.setattr(emscreen, "hk_statements", lambda s, codes, a, b: {})
    msgs = universe.refresh_hk(app)
    assert "港股通 1 只" in msgs[0] and "2 只" in msgs[0]
    rows = {u["code"]: u for u in store.universe(app.db, "HK")}
    assert set(rows) == {"00001", "08001"}
    assert rows["00001"]["info"]["in_connect"] is True and rows["08001"]["info"]["in_connect"] is False
    assert universe.in_hk_scope(rows["08001"], "all") and not universe.in_hk_scope(rows["08001"], "connect")
    assert universe.in_hk_scope(rows["00001"], "connect")
    assert universe.in_hk_scope({"market": "A", "info": {}}, "connect")
    assert universe.in_hk_scope({"market": "HK", "info": {}}, "connect")  # 旧快照没有标记：当时只有港股通


def test_run_hk_scope_filters_non_connect(seeded):
    app = seeded
    other = uni(secid="116.08001", code="08001", name="非通牛", info={"in_connect": False})
    seed(app, [*store.universe(app.db, "HK"), other])  # upsert 整体替换某个市场的快照
    assert any(h["code"] == "00001" for h in store.universe(app.db, "HK"))
    all_ = sc_run.run(app, ("long",), use_llm=False, refresh=False, progress=lambda m: None)
    assert any(h["code"] == "08001" for h in store.hits(app.db, all_.runs["long"]))
    run = store.runs(app.db, "long", 1)[0]
    assert run["params"]["hk_scope"] == "all"
    only = sc_run.run(app, ("long",), use_llm=False, refresh=False, progress=lambda m: None, hk_scope="connect")
    assert all(h["code"] != "08001" for h in store.hits(app.db, only.runs["long"]))
    assert any("仅港股通" in m for m in only.msgs)
    assert store.runs(app.db, "long", 1)[0]["params"]["hk_scope"] == "connect"
    app.settings.screen.hk_scope = "connect"  # 不指定时用配置
    cfg_run = sc_run.run(app, ("long",), use_llm=False, refresh=False, progress=lambda m: None)
    assert all(h["code"] != "08001" for h in store.hits(app.db, cfg_run.runs["long"]))


def test_hk_scope_job_flag_and_page_filter(seeded, project):
    from fastapi.testclient import TestClient

    from stockagent.web.server import create_app

    steps, _, _ = jobs.build(seeded.watchlist, seeded.settings.llm.profiles, "screen_run", flags=["--hk-scope=connect", "--no-llm"])
    assert steps == [["screen", "run", "--hk-scope=connect", "--no-llm"]]
    with pytest.raises(jobs.JobError):
        jobs.build(seeded.watchlist, seeded.settings.llm.profiles, "screen_run", flags=["--hk-scope=bogus"])
    seed(seeded, [*store.universe(seeded.db, "HK"), uni(secid="116.08001", code="08001", name="非通牛", info={"in_connect": False})])
    sc_run.run(seeded, ("long",), use_llm=False, refresh=False, progress=lambda m: None)
    web = create_app(project / "config.yaml", allowed_hosts=["testserver"], start_background=False)
    client = TestClient(web)
    client.get("/")
    html = client.get("/screen?show=all&scope=all").text
    assert "<b>非通牛</b>" in html and "<b>现金牛</b>" in html and "非港股通" in html
    only_other = client.get("/screen?show=all&scope=hko").text
    assert "<b>非通牛</b>" in only_other and "<b>现金牛</b>" not in only_other
    assert "<b>非通牛</b>" not in client.get("/screen?show=all&scope=hkc").text
    assert "<b>甲公司</b>" not in client.get("/screen?show=all&scope=hk").text
    assert client.get("/screen?scope=nope").status_code == 404
    # 运行表单把港股范围变成任务开关
    r = client.post("/screen/run", data={"csrf_token": client.cookies.get("stock_csrf"), "pool": "long", "hk_scope": "connect"},
                    follow_redirects=False)
    assert r.status_code == 303
    job = web.state.holder.app.db.jobs(1)[0]
    assert job["task"] == "screen_run" and "--hk-scope=connect" in str(job["argv"])
