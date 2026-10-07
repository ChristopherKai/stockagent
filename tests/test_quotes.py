"""行情刷新与情景切换：开盘判断、现价展示状态、情景归属与滞后、切换事件与推送、刷新不抹掉南向持股、调度器进程内执行、
页面上的现价 / 横幅 / 报告日价格。全部离线（时间由测试传入或替换 marketclock.now）。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from test_tracker import CARD, _bars, save_dossier, tapp  # noqa: F401 - fixture
from test_web import post, web  # noqa: F401 - fixture

from stockagent import jobs, marketclock, notify, quotes, scenario_watch, scheduler, tracker
from stockagent import valuation as V
from stockagent.config import ScheduleJob
from stockagent.sources import eastmoney
from stockagent.utils import CST, NY, iso
from stockagent.web.views import _report_price, watch_rows

THU = date(2026, 10, 8)       # 周四：港股开市，A 股仍在国庆休市（到 10-07 止，10-08 恢复），美股开市
HK_SECID = "116.00189"


def cst(h, m=0, d=THU):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=CST)


def put_quote(app, secid, data, taken):
    app.db._exec("INSERT INTO quotes(secid, taken_at, data_json) VALUES(?,?,?) ON CONFLICT(secid) DO UPDATE SET "
                 "taken_at=excluded.taken_at, data_json=excluded.data_json", (secid, iso(taken), json.dumps(data)))


def put_bars(app, secid, rows):
    app.db._exec("DELETE FROM bars WHERE secid=?", (secid,))
    app.db.upsert_bars(secid, [{"date": d, "open": c, "close": c, "high": c, "low": c, "volume": 1e6, "amount": None} for d, c in rows])


# ---------------------------------------------------------------- 开盘判断

def test_market_sessions_a_and_hk():
    assert marketclock.is_open("A", cst(9, 30)) and marketclock.is_open("A", cst(11, 29)) and marketclock.is_open("A", cst(14, 59))
    assert not marketclock.is_open("A", cst(9, 29)) and not marketclock.is_open("A", cst(12, 0)) and not marketclock.is_open("A", cst(15, 0))
    assert marketclock.is_open("HK", cst(11, 59)) and marketclock.is_open("HK", cst(12, 0)) is False       # 午休：港股 12:00–13:00
    assert marketclock.is_open("HK", cst(13, 0)) and marketclock.is_open("HK", cst(15, 59)) and not marketclock.is_open("HK", cst(16, 0))
    assert marketclock.is_open("A", cst(13, 0)) and not marketclock.is_open("A", cst(11, 30))
    sat = date(2026, 10, 10)
    assert not marketclock.is_open("HK", cst(10, 0, sat)) and not marketclock.is_open("US", datetime(2026, 10, 10, 10, 0, tzinfo=NY))


def test_market_closed_on_holidays_and_half_days():
    assert not marketclock.is_open("A", cst(10, 0, date(2026, 10, 7)))          # 国庆休市
    assert marketclock.is_open("A", cst(10, 0, date(2026, 10, 8)))
    assert not marketclock.is_open("HK", cst(10, 0, date(2026, 10, 1)))
    half = date(2026, 12, 24)                                                   # 港股平安夜半日市：只有上午盘
    assert marketclock.is_open("HK", cst(11, 59, half)) and not marketclock.is_open("HK", cst(12, 0, half))
    assert not marketclock.is_open("HK", cst(13, 30, half))
    assert marketclock.day_close("HK", half) == cst(12, 0, half) and marketclock.day_close("HK", THU) == cst(16, 0)
    assert marketclock.day_close("A", date(2026, 10, 7)) is None


def test_us_session_follows_daylight_saving():
    summer, winter = date(2026, 7, 15), date(2026, 12, 15)
    utc = lambda y, mo, d, h, mi: datetime(y, mo, d, h, mi, tzinfo=timezone.utc)  # noqa: E731
    assert marketclock.is_open("US", utc(2026, 7, 15, 13, 30)) and not marketclock.is_open("US", utc(2026, 7, 15, 13, 29))      # 夏令时 EDT = UTC-4
    assert marketclock.is_open("US", utc(2026, 7, 15, 19, 59)) and not marketclock.is_open("US", utc(2026, 7, 15, 20, 0))
    assert marketclock.is_open("US", utc(2026, 12, 15, 14, 30)) and not marketclock.is_open("US", utc(2026, 12, 15, 14, 29))    # 冬令时 EST = UTC-5
    assert marketclock.is_open("US", utc(2026, 12, 15, 20, 59)) and not marketclock.is_open("US", utc(2026, 12, 15, 21, 0))
    assert marketclock.local_day("US", utc(2026, 7, 16, 3, 0)) == summer      # 北京时间 07-16 11:00 仍是美东 07-15
    assert marketclock.day_close("US", winter).astimezone(CST) == datetime(2026, 12, 16, 5, 0, tzinfo=CST)


def test_refresh_window_includes_a_grace_after_close():
    assert marketclock.in_refresh_window("HK", cst(10, 0)) and marketclock.in_refresh_window("HK", cst(16, 5))
    assert not marketclock.in_refresh_window("HK", cst(16, 12)) and not marketclock.in_refresh_window("HK", cst(12, 30))
    assert marketclock.in_refresh_window("A", cst(15, 8)) and not marketclock.in_refresh_window("A", cst(8, 0))
    assert marketclock.is_final("HK", cst(16, 5)) and not marketclock.is_final("HK", cst(10, 0))
    assert marketclock.is_final("HK", cst(10, 0, date(2026, 10, 10)))          # 周六取到的快照是周五收盘价


# ---------------------------------------------------------------- 现价展示

def _hk(app):
    return app.watchlist.get("acme").listing("HK")


def test_display_price_live_and_stale(app):
    hk = _hk(app)
    sid = app.secid(hk)
    put_bars(app, sid, [("2026-10-06", 10.0), ("2026-10-07", 10.2)])
    put_quote(app, sid, {"price": 10.5, "prev_close": 10.2, "change_pct": 0.0294}, cst(10, 28))
    px = V.display_price(app, hk, cst(10, 30))
    assert (px["status"], px["price"], px["change_pct"], px["time"], px["stale"]) == ("live", 10.5, 0.0294, "10:28", False)
    px = V.display_price(app, hk, cst(11, 0))                                  # 28 → 32 分钟没更新：过期
    assert px["status"] == "live" and px["stale"]
    put_quote(app, sid, {"price": 10.5, "prev_close": 10.2}, cst(10, 28))      # 没有 change_pct：用昨收算
    assert V.display_price(app, hk, cst(10, 30))["change_pct"] == pytest.approx(10.5 / 10.2 - 1)
    # 开盘了但快照还是前一天的：退回收盘价，不显示盘中
    put_quote(app, sid, {"price": 9.9, "change_pct": 0.1}, cst(16, 5, date(2026, 10, 7)))
    px = V.display_price(app, hk, cst(9, 35))
    assert (px["status"], px["price"], px["day"], px["source"]) == ("close", 10.2, "2026-10-07", "bar")


def test_display_price_after_close(app):
    hk = _hk(app)
    sid = app.secid(hk)
    put_bars(app, sid, [("2026-10-06", 10.0), ("2026-10-07", 10.2)])
    # 收盘后取到的快照比日线新（日线晚上才抓）：用快照价与快照涨跌幅，日期是 10-08
    put_quote(app, sid, {"price": 10.8, "prev_close": 10.2, "change_pct": 0.0588}, cst(16, 5))
    px = V.display_price(app, hk, cst(17, 0))
    assert (px["status"], px["price"], px["change_pct"], px["day"], px["source"]) == ("close", 10.8, 0.0588, "2026-10-08", "quote")
    # 日线已经抓到当天：价格取日线收盘；涨跌幅优先用收盘后的快照
    put_bars(app, sid, [("2026-10-07", 10.2), ("2026-10-08", 10.8)])
    px = V.display_price(app, hk, cst(19, 0))
    assert (px["price"], px["change_pct"], px["day"], px["source"]) == (10.8, 0.0588, "2026-10-08", "bar")
    # 快照是盘中取的（收盘前）：涨跌幅不能用，按两根日线收盘价算
    put_quote(app, sid, {"price": 10.6, "change_pct": 0.039}, cst(10, 0))
    px = V.display_price(app, hk, cst(19, 0))
    assert px["change_pct"] == pytest.approx(10.8 / 10.2 - 1) and px["status"] == "close"
    # 没有快照：退回日线，不显示盘中，休市日（周六）也一样
    app.db._exec("DELETE FROM quotes")
    px = V.display_price(app, hk, cst(10, 0))
    assert (px["status"], px["price"], px["taken_at"]) == ("close", 10.8, None) and px["change_pct"] == pytest.approx(10.8 / 10.2 - 1)
    assert V.display_price(app, hk, cst(10, 0, date(2026, 10, 10)))["status"] == "close"


def test_display_price_weekend_quote_dated_last_trading_day(app):
    hk = _hk(app)
    sid = app.secid(hk)
    put_bars(app, sid, [("2026-10-07", 10.2)])
    put_quote(app, sid, {"price": 10.8, "change_pct": 0.0588}, cst(16, 5, THU))     # 周四收盘后取
    px = V.display_price(app, hk, cst(11, 0, date(2026, 10, 10)))                   # 周六查看，日线还没补周四
    assert px["price"] == 10.8 and px["day"] == "2026-10-08"
    put_quote(app, sid, {"price": 10.9, "change_pct": 0.01}, cst(9, 0, date(2026, 10, 10)))   # 周六取的：日期回退到周五
    assert V.display_price(app, hk, cst(11, 0, date(2026, 10, 10)))["day"] == "2026-10-09"


def test_display_price_nothing(app):
    px = V.display_price(app, _hk(app), cst(10, 0))
    assert px["price"] is None and px["status"] is None and not px["stale"]


def test_listing_views_use_display_price_for_stats(tapp):  # noqa: F811
    c = tapp.watchlist.get("dongyue")
    save_dossier(tapp)
    tracker.add(tapp, c)
    put_bars(tapp, HK_SECID, [("2026-10-07", 11.0), ("2026-10-08", 11.05)])
    put_quote(tapp, HK_SECID, {"price": 12.0, "prev_close": 11.0, "change_pct": 0.09}, cst(10, 28))
    from unittest import mock
    with mock.patch.object(marketclock, "now", lambda: cst(10, 30)):
        v = tracker.status(tapp, c)["views"][0]
        assert v["price"] == 12.0 and v["px"]["status"] == "live" and v["px"]["change_pct"] == 0.09
        assert v["upside"] == pytest.approx(v["target"] / 12.0 - 1) and v["stats"]["price"] == 12.0   # 现价与空间用同一个价
        assert tracker.status(tapp, c)["price"] == 12.0
    with mock.patch.object(marketclock, "now", lambda: cst(17, 0)):
        assert tracker.status(tapp, c)["views"][0]["price"] == 11.05


# ---------------------------------------------------------------- 情景归属与滞后

def _range_model(scale=1.0):
    return V.ValuationModel(method="range", driver_label="价", multiple_label="—", shares=1, price_currency="HKD", listing="HK:00189",
                            scenarios=[V.ScenarioPoint(name="bull", driver=16 * scale, multiple=1, probability=0.3, low=15, high=17, drivers_text="牛驱动"),
                                       V.ScenarioPoint(name="base", driver=12.5 * scale, multiple=1, probability=0.5, low=12, high=13, drivers_text="基驱动"),
                                       V.ScenarioPoint(name="bear", driver=8.5 * scale, multiple=1, probability=0.2, low=8, high=9, drivers_text="熊驱动")])


def test_scenario_assignment_intervals_gaps_and_outside():
    m = _range_model()
    assert [scenario_watch.assign(m, p) for p in (16, 12.5, 8.2)] == ["bull", "base", "bear"]
    assert scenario_watch.intervals(m)["base"] == (12.0, 13.0, 12.5)
    assert scenario_watch.assign(m, 13.9) == "base" and scenario_watch.assign(m, 14.1) == "bull"      # 空隙 13–15 以中点 14 为界
    assert scenario_watch.assign(m, 10.4) == "bear" and scenario_watch.assign(m, 10.6) == "base"      # 空隙 9–12，中点 10.5
    assert scenario_watch.assign(m, 30) == "bull" and scenario_watch.assign(m, 3) == "bear"           # 区间之外归给最近的一端
    pt = V.ValuationModel(method="range", driver_label="价", multiple_label="—", shares=1, price_currency="HKD", listing="HK:00189",
                          scenarios=[V.ScenarioPoint(name="bull", driver=20, multiple=1, probability=0.5),
                                     V.ScenarioPoint(name="bear", driver=10, multiple=1, probability=0.5)])
    assert scenario_watch.intervals(pt)["bear"] == pytest.approx((9.0, 11.0, 10.0))                   # 没有区间：单点取情景价 ±10% 作隐含区间
    assert scenario_watch.assign(pt, 14.4) == "bear" and scenario_watch.assign(pt, 14.6) == "bull"    # 空隙 11–18，中点 14.5
    assert scenario_watch.intervals(_range_model(0.5))["bull"][:2] == (7.5, 8.5)                       # 区间随情景价同比缩放


def test_scenario_zone_hysteresis():
    m = _range_model()
    assert scenario_watch.zone(m, 15.05, "base") == "base"       # 刚进入牛市区间 15–17（不足 1%）：不算切换
    assert scenario_watch.zone(m, 15.2, "base") == "bull"
    assert scenario_watch.zone(m, 14.95, "bull") == "bull" and scenario_watch.zone(m, 14.2, "bull") == "bull"   # 掉进空隙：仍算原情景
    assert scenario_watch.zone(m, 12.5, "bull") == "base" and scenario_watch.zone(m, 12.95, "bull") == "bull"   # 进入基准区间 12–13（且越过边界 1%）才算
    assert scenario_watch.zone(m, 14.05, None) == "bull"          # 没有历史状态：空隙按中点归属
    assert scenario_watch.zone(m, 10.4, "base") == "base" and scenario_watch.zone(m, 10.6, "bear") == "bear"   # 空隙里不切换
    assert scenario_watch.zone(m, 8.5, "base") == "bear" and scenario_watch.zone(m, 3, "base") == "bear"      # 进入熊市区间 / 跌穿区间下沿
    assert scenario_watch.zone(m, 10.4, "bull") == "bear"         # prev 不与空隙相邻（牛 → 空隙在熊/基准之间）：按中点归属


def test_point_scenarios_gap_is_not_a_switch():
    """回归（剑桥科技）：程序定价的情景是单点 88.2 / 144.9 / 213.4，现价 115.1 离熊价很远，不能因越过两价格中点 116.6 就报「进入熊市区间」。"""
    pts = [V.ScenarioPoint(name=n, driver=v, multiple=1, probability=p) for n, v, p in (("bull", 213.4, 0.25), ("base", 144.9, 0.6), ("bear", 88.2, 0.15))]
    m = V.ValuationModel(method="range", driver_label="价", multiple_label="—", shares=1, price_currency="HKD", listing="HK:06166", scenarios=pts)
    assert scenario_watch.assign(m, 115.1) == "base"                                    # 无状态时的展示归属按空隙中点（97.0 与 130.4 之间，113.7）
    assert scenario_watch.zone(m, 115.1, "base") == "base" and scenario_watch.zone(m, 100.0, "base") == "base"
    assert scenario_watch.zone(m, 96.0, "base") == "bear"                               # 真正进入熊价 ±10%（79.4–97.0）才算
    assert scenario_watch.zone(m, 115.1, "bear") == "bear" and scenario_watch.zone(m, 135.0, "bear") == "base"


# ---------------------------------------------------------------- 切换事件与推送

@pytest.fixture
def tracked(tapp, monkeypatch):  # noqa: F811
    save_dossier(tapp)
    tracker.add(tapp, tapp.watchlist.get("dongyue"))
    put_bars(tapp, HK_SECID, [("2026-10-07", 11.0), ("2026-10-08", 11.05)])
    sent = []
    monkeypatch.setattr(notify, "push", lambda settings, title, text: sent.append((title, text)) or ["飞书：ok"])
    monkeypatch.setattr(marketclock, "now", lambda: cst(17, 0))
    tapp.sent = sent
    return tapp


def test_first_check_records_only_then_switch_pushes(tracked):
    a, c = tracked, tracked.watchlist.get("dongyue")
    assert scenario_watch.check(a, [c]) == [] and a.sent == [] and a.db.scenario_events() == []   # 第一次只记录
    state = json.loads(a.db.kv_get("scen_zone:dongyue"))
    assert state["zone"] == "bear" and state["price"] == 11.05 and state["rev"] == a.db.tracker("dongyue")["revision_id"]
    assert scenario_watch.check(a, [c]) == [] and a.db.scenario_events() == []                    # 没变化
    put_bars(a, HK_SECID, [("2026-10-07", 11.05), ("2026-10-08", 20.0)])                          # 收盘涨到牛市区间
    msgs = scenario_watch.check(a, [c])
    assert "熊市 → 牛市" in msgs[0] and "价格" in msgs[0] and len(a.sent) == 1
    ev = a.db.scenario_events(unread_only=True)
    assert len(ev) == 1 and (ev[0]["old_scenario"], ev[0]["new_scenario"], ev[0]["reason"]) == ("bear", "bull", "price")
    assert ev[0]["price"] == 20.0 and ev[0]["change_pct"] == pytest.approx(20.0 / 11.05 - 1) and ev[0]["listing"] == "00189.HK"
    assert ev[0]["snapshot"]["scenarios"]["bull"]["price"] > 20 and ev[0]["snapshot"]["target"] > 0
    title, text = a.sent[0]
    assert title.startswith("【情景切换】") and "东岳集团" in title
    for needle in ("东岳集团（00189.HK）", "熊市 → 牛市", "现价 20.00 HKD", "日涨跌 +", "牛市 23.50（+18%）", "基准 15.67", "熊市 9.84",
                   "目标价（情景加权估值）", "报告立场：偏多", "▶ 新进入的牛市情景驱动：长协上行", "情景加权估值，非投资建议"):
        assert needle in text, needle
    assert "基准情景驱动：长协持平" in text and "配额调减" in text     # 其他情景的驱动也带上（较短）
    assert scenario_watch.check(a, [c]) == [] and len(a.db.scenario_events()) == 1               # 同一状态不重复通知
    assert a.db.mark_scenario_events_read(ev[0]["id"]) == 1 and not a.db.scenario_events(unread_only=True)


def test_no_push_writes_event_only(tracked):
    a, c = tracked, tracked.watchlist.get("dongyue")
    scenario_watch.check(a, [c], push=False)
    put_bars(a, HK_SECID, [("2026-10-07", 11.05), ("2026-10-08", 20.0)])
    msgs = scenario_watch.check(a, [c], push=False)
    assert msgs and a.sent == [] and len(a.db.scenario_events()) == 1


def test_valuation_revision_changes_scenario(tracked):
    a, c = tracked, tracked.watchlist.get("dongyue")
    scenario_watch.check(a, [c])                                       # 价格 11.05 在熊市区间
    rev = a.db.latest_revision("dongyue")
    model = json.loads(json.dumps(rev["model"]))
    for s in model["scenarios"]:                                        # 估值整体下修一半：现价没动，却落进了新的牛市区间
        s["driver"] *= 0.5
        s["low"] = (s.get("low") or 0) * 0.5 or None
        s["high"] = (s.get("high") or 0) * 0.5 or None
    new = {k: v for k, v in rev.items() if k not in ("id",)}
    new.update(kind="revision", model=model, summary="下修", created_at=iso(datetime.now(timezone.utc)))
    a.db.add_revision(new)
    msgs = scenario_watch.check(a, [c])
    ev = a.db.scenario_events()
    assert ev and ev[0]["reason"] == "valuation" and ev[0]["revision_id"] == a.db.tracker("dongyue")["revision_id"], msgs
    assert "估值修正后" in msgs[0] and "估值修正后" in a.sent[0][0]
    assert scenario_watch.check(a, [c]) == []


def test_stale_live_price_and_untracked_are_skipped(tracked):
    a, c = tracked, tracked.watchlist.get("dongyue")
    put_quote(a, HK_SECID, {"price": 20.0}, cst(10, 0))
    import unittest.mock as mock
    with mock.patch.object(marketclock, "now", lambda: cst(11, 0)):     # 盘中快照 60 分钟没更新
        assert scenario_watch.check(a, [c]) == [] and a.db.kv_get("scen_zone:dongyue") is None
    a.db.set_tracker("dongyue", enabled=0)
    assert scenario_watch.check(a, [c]) == [] and a.db.kv_get("scen_zone:dongyue") is None


# ---------------------------------------------------------------- 行情刷新

def test_merge_quote_keeps_old_fields():
    old = {"price": 10.0, "southbound_ratio": 3.2, "southbound_chg_20d": 0.4, "southbound_date": "2026-10-07", "pe_ttm": 8.0,
           "lot": 1000, "taken_at": "x"}
    new = {"price": 10.5, "prev_close": 10.0, "change_pct": 0.05, "pe_ttm": 8.4}
    m = quotes.merge_quote(old, new)
    assert m["price"] == 10.5 and m["pe_ttm"] == 8.4 and m["southbound_ratio"] == 3.2 and m["lot"] == 1000 and "taken_at" not in m
    assert quotes.merge_quote(None, new) == new


def test_refresh_keeps_southbound_and_skips_closed_markets(tracked, monkeypatch):
    a, c = tracked, tracked.watchlist.get("dongyue")
    put_quote(a, HK_SECID, {"price": 11.0, "southbound_ratio": 3.2, "southbound_chg_20d": 0.4, "southbound_date": "2026-10-07", "pe_ttm": 8.0},
              cst(9, 0, date(2026, 10, 7)))
    calls = []
    monkeypatch.setattr(eastmoney, "fetch_quote", lambda s, secid: calls.append(secid) or {"price": 11.4, "prev_close": 11.0, "change_pct": 0.036})
    assert quotes.refresh(a, [c], push=False, ts=cst(8, 0)) == [] and calls == []        # 开盘前：不联网
    assert quotes.refresh(a, [c], push=False, ts=cst(12, 30)) == [] and calls == []      # 午休
    msgs = quotes.refresh(a, [c], push=False, ts=cst(10, 0))
    assert calls == [HK_SECID] and "1/1" in msgs[0]
    q = a.db.quote(HK_SECID)
    assert q["price"] == 11.4 and q["change_pct"] == 0.036 and q["pe_ttm"] == 8.0
    assert (q["southbound_ratio"], q["southbound_chg_20d"], q["southbound_date"]) == (3.2, 0.4, "2026-10-07")   # 南向持股没被抹掉
    assert a.db.quote(HK_SECID)["taken_at"] > iso(datetime.now(timezone.utc) - timedelta(minutes=1))
    quotes.refresh(a, [c], push=False, ts=cst(16, 5))                                    # 刚收盘：再取一次收盘价
    assert len(calls) == 2


def test_refresh_only_tracked_and_reports_failures(tracked, monkeypatch):
    a = tracked
    monkeypatch.setattr(eastmoney, "fetch_quote", lambda s, secid: None)
    from stockagent.sources import tencent
    monkeypatch.setattr(tencent, "fetch_quote", lambda s, secid: None)
    msgs = quotes.refresh(a, list(a.watchlist.companies), push=False, ts=cst(10, 0))
    assert "0/1" in msgs[0] and "00189.HK" in msgs[0]
    a.db.set_tracker("dongyue", enabled=0)
    assert quotes.refresh(a, list(a.watchlist.companies), push=False, ts=cst(10, 0)) == []     # 没有追踪中的公司


def test_refresh_triggers_scenario_check(tracked, monkeypatch):
    a, c = tracked, tracked.watchlist.get("dongyue")
    monkeypatch.setattr(marketclock, "now", lambda: cst(10, 30))
    put_quote(a, HK_SECID, {"price": 11.0}, cst(10, 29))
    scenario_watch.check(a, [c])                                                           # 记录：熊市
    monkeypatch.setattr(eastmoney, "fetch_quote", lambda s, secid: {"price": 20.0, "prev_close": 11.0, "change_pct": 0.81})
    msgs = quotes.refresh(a, [c], push=True, ts=cst(10, 30))
    assert any("熊市 → 牛市" in m for m in msgs) and len(a.sent) == 1 and a.db.scenario_events(unread_only=True)


# ---------------------------------------------------------------- 调度器 / 任务登记

def test_quotes_refresh_task_registered_and_run_inline_by_scheduler(tapp, monkeypatch):  # noqa: F811
    t = jobs.TASKS["quotes_refresh"]
    assert t.inline and t.flags == ("--no-push",) and t.companies == "any" and t.group is None
    steps, title, _ = jobs.build(tapp.watchlist, tapp.settings.llm.profiles, "quotes_refresh", flags=["--no-push"])
    assert steps == [["quotes", "refresh", "--no-push"]] and title == "行情刷新"
    tapp.settings.scheduler.jobs = [ScheduleJob(name="行情刷新", task="quotes_refresh", args=["--no-push"], cron="*/5 * * * *")]
    seen = []
    monkeypatch.setattr(quotes, "run_scheduled", lambda app, companies, flags: seen.append(([c.id for c in companies], flags)) or [])
    t0 = cst(10, 1)
    assert scheduler.tick(tapp, t0, wait_inline=True) == []                                # 首次见到：从当前分钟开始计
    assert scheduler.tick(tapp, t0 + timedelta(minutes=5), wait_inline=True) == []
    assert seen == [([c.id for c in tapp.watchlist.companies], ["--no-push"])]
    assert tapp.db.jobs() == []                                                            # 不落 jobs 记录
    tapp.settings.scheduler.jobs[0].args = ["--bogus"]
    msgs = scheduler.tick(tapp, t0 + timedelta(minutes=10), wait_inline=True)
    assert msgs and "配置有误" in msgs[0]


def test_inline_task_skips_when_previous_round_running(tapp, monkeypatch):  # noqa: F811
    tapp.settings.scheduler.jobs = [ScheduleJob(name="行情刷新", task="quotes_refresh", cron="*/5 * * * *")]
    seen = []
    monkeypatch.setattr(quotes, "run_scheduled", lambda *a: seen.append(1) or [])
    scheduler._inline_running.add("quotes_refresh")
    try:
        scheduler.tick(tapp, cst(10, 0), wait_inline=True)
        scheduler.tick(tapp, cst(10, 5), wait_inline=True)
        assert seen == []
    finally:
        scheduler._inline_running.discard("quotes_refresh")


def test_cli_quotes_refresh(tapp, monkeypatch, capsys):  # noqa: F811
    from stockagent import cli
    monkeypatch.setattr(quotes, "refresh", lambda app, companies, push=True, ts=None: [f"push={push} n={len(companies)}"])
    monkeypatch.setattr(cli, "_load", lambda args: tapp)
    cli.main(["quotes", "refresh", "--no-push"])
    assert "push=False n=" in capsys.readouterr().out
    monkeypatch.setattr(quotes, "refresh", lambda *a, **k: [])
    cli.main(["quotes", "refresh"])
    assert "无需刷新" in capsys.readouterr().out


# ---------------------------------------------------------------- 页面

def test_dashboard_price_cell_states(web, monkeypatch):  # noqa: F811
    a = web.holder.app
    put_quote(a, HK_SECID, {"price": 11.3, "prev_close": 11.0, "change_pct": 0.0273}, cst(10, 28))
    monkeypatch.setattr(marketclock, "now", lambda: cst(10, 30))
    html = web.get("/").text
    assert "<b>11.30</b>" in html and "+2.73%" in html and "盘中 10:28" in html and "行情未更新" not in html
    assert 'class="up">+2.73%' in html and '<b>11.30</b> <span class="muted small">HKD</span>' in html                                               # 涨跌色沿用 up / down
    monkeypatch.setattr(marketclock, "now", lambda: cst(11, 30))
    stale = web.get("/").text
    assert "px-stale" in stale and "行情未更新" in stale
    put_quote(a, HK_SECID, {"price": 10.7, "prev_close": 11.0, "change_pct": -0.0273}, cst(16, 5))
    monkeypatch.setattr(marketclock, "now", lambda: cst(17, 0))
    closed = web.get("/").text
    assert "收盘 10-08" in closed and "盘中" not in closed and "<b>10.70</b>" in closed and 'class="down">-2.73%' in closed
    assert "<th class=\"r\">现价</th>" not in web.get("/companies").text and "<b>10.70</b>" not in web.get("/companies").text   # 观察清单没有现价列


def test_banner_on_dashboard_and_watchlist_with_acknowledge(web, monkeypatch):  # noqa: F811
    a = web.holder.app
    monkeypatch.setattr(marketclock, "now", lambda: cst(17, 0))
    monkeypatch.setattr(notify, "push", lambda *x: [])
    c = a.watchlist.get("dongyue")
    put_bars(a, HK_SECID, [("2026-10-07", 11.0), ("2026-10-08", 11.05)])
    scenario_watch.check(a, [c])
    put_bars(a, HK_SECID, [("2026-10-07", 11.05), ("2026-10-08", 20.0)])
    scenario_watch.check(a, [c])
    for url in ("/", "/companies"):
        html = web.get(url).text
        assert "情景切换" in html and "东岳集团" in html and "熊市 → <b class=\"scn-bull\">牛市</b>" in html and "知道了" in html, url
        assert "/scenario-events/1/read" in html
    r = post(web, "/scenario-events/1/read", {"next": "/companies"})
    assert r.status_code == 303 and r.headers["location"].startswith("/companies")
    assert "scen-banner" not in web.get("/").text and a.db.scenario_events()[0]["read_at"]
    assert post(web, "/scenario-events/read-all", {"next": "//evil.example"}).headers["location"].startswith("/")   # 只接受站内路径
    assert not post(web, "/scenario-events/read-all", {"next": "//evil.example"}).headers["location"].startswith("//")


def test_watchlist_report_day_price(web, monkeypatch):  # noqa: F811
    a = web.holder.app
    monkeypatch.setattr(marketclock, "now", lambda: cst(17, 0))
    put_bars(a, HK_SECID, [("2026-10-07", 11.0), ("2026-10-08", 12.0)])
    d = a.db.latest_dossier("dongyue")
    c = a.watchlist.get("dongyue")
    # 口径与报告历史页一致：跟踪卡 stats.price → 基线修正的 price → 报告日（含）之前最近的收盘价
    a.db._exec("UPDATE dossiers SET card_json=? WHERE id=?", (json.dumps({**d["card"], "stats": {"price": 10.0}}), d["id"]))
    rp = watch_rows(a)[0]["report_px"]
    assert rp["price"] == 10.0 and rp["now"] == 12.0 and rp["since"] == pytest.approx(0.2) and rp["listing"].key == "HK:00189"
    a.db._exec("UPDATE dossiers SET card_json=? WHERE id=?", (json.dumps(d["card"]), d["id"]))
    base = a.db.latest_revision("dongyue", kind="baseline")
    assert _report_price(a, c, a.db.dossier(d["id"]))["price"] == base["price"]
    a.db._exec("UPDATE dossiers SET created_at=? WHERE id=?", (iso(datetime(2026, 10, 7, 9, 0, tzinfo=CST)), d["id"]))
    a.db._exec("UPDATE revisions SET price=NULL")
    rp = _report_price(a, c, a.db.dossier(d["id"]), baselines={})
    assert rp["price"] == 11.0 and rp["day"] == "2026-10-07"                                  # 取报告日的收盘价
    html = web.get("/companies").text
    assert "报告日价格 / 至今" in html and "11.00" in html and "+9.1%" in html
    assert "报告日价格" in web.get("/c/dongyue/reports").text                                  # 报告历史页沿用同一个函数
