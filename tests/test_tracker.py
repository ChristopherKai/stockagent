"""标的追踪：代码建基线、依据收集与去重、修正的校验与应用、自动重跑深度报告、与 run / 简报 / 锁的集成。"""

from __future__ import annotations

import textwrap
from datetime import timedelta

import pandas as pd
import pytest

from stockagent import brief, cli, monitor, tracker
from stockagent.app import App, _item
from stockagent.llm import LLMResult
from stockagent.tools import ToolBox, matrix_table
from stockagent.triage import short_id
from stockagent.utils import iso, utcnow

WATCHLIST = """
companies:
  - id: dongyue
    name: 东岳集团
    listings: [{market: HK, code: "00189", benchmark: "100.HSI"}]
benchmarks:
  HK: [{secid: "100.HSI", name: 恒生指数}]
"""
MATRIX = {"driver_label": "2027E 归母净利（亿 CNY）", "driver_values": [16, 18, 20, 22, 24, 26], "multiple_label": "PE",
          "multiples": [6, 8, 10, 12, 14], "shares": 17.33, "net_cash": 60.37, "fx": 1.169, "current_price": 11.05}
CARD = {"thesis": "净现金与硅材股权支撑估值，配额红利未被定价", "stance": "bullish",
        "scenarios": [{"name": "bull", "price_low": 21.88, "price_high": 25.12, "probability": 0.25, "drivers": "长协上行"},
                      {"name": "base", "price_low": 13.79, "price_high": 17.56, "probability": 0.5, "drivers": "长协持平"},
                      {"name": "bear", "price_low": 9.12, "price_high": 10.55, "probability": 0.25, "drivers": "配额调减"}],
        "watch_metrics": [{"name": "R32 现货价", "current": "65,500 元/吨", "threshold": "跌破 60,000", "source": "隆众", "manual": False}],
        "data_gaps": ["2027 年配额正式稿"]}
REPORT = "# 东岳\n\n## § 核心洞见\n配额是行政性供给刚性。\n\n## § 12 个月情景\n基准 18–20 亿 × 8–10×。\n\n## § 边栏\n无"


def _bars(app, secid, closes):
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=len(closes)).strftime("%Y-%m-%d")
    app.db.upsert_bars(secid, [{"date": d, "open": c, "close": c, "high": c, "low": c, "volume": 1e6, "amount": None}
                               for d, c in zip(dates, closes)])


def save_dossier(app, card=CARD, created_at=None, triggers=()) -> int:
    log = [{"tool": "valuation_matrix", "args": MATRIX, "result": matrix_table(
        MATRIX["driver_label"], MATRIX["driver_values"], "PE", MATRIX["multiples"], MATRIX["shares"], MATRIX["net_cash"], MATRIX["fx"])}]
    did = app.db.save_dossier("dongyue", "mock", REPORT, card, {}, log, list(triggers),
                              [{"when": "2026-12", "due_date": "2026-12-01", "event": "2027 年配额正式稿", "kpi": "维持 18.53 亿 tCO₂"}],
                              {"related_tickers": [], "news_keywords": []})
    if created_at:
        app.db._exec("UPDATE dossiers SET created_at=? WHERE id=?", (created_at, did))
    return did


def seed_event(app, title, materiality=4, direction="positive", published=None, uid_seed=None) -> str:
    c = app.watchlist.get("dongyue")
    x = {"source_id": uid_seed or title, "title": title, "published_at": published or utcnow() - timedelta(hours=1),
         "url": "https://www1.hkexnews.hk/x.pdf"}
    it = _item(c, c.listings[0], "announcement", "hkex", x, "公告")
    app.db.add_item(it)
    app.db.save_triage(it["uid"], {"relevant": True, "duplicate_of": "", "event_type": "orders_customers", "direction": direction,
                                   "materiality": materiality, "novelty": "new", "horizon": "weeks", "summary_zh": title,
                                   "expectation": "超预期", "follow_up": ""}, "mock")
    return it["uid"]


@pytest.fixture
def tapp(project) -> App:
    (project / "watchlist.yaml").write_text(textwrap.dedent(WATCHLIST), encoding="utf-8")
    a = App.load(project / "config.yaml")
    _bars(a, "116.00189", [11.05] * 30)
    _bars(a, "100.HSI", [24000.0] * 30)
    _bars(a, "133.HKDCNH", [0.85541] * 10)
    _bars(a, "133.USDCNH", [6.7105] * 10)
    a.db.save_quote("116.00189", {"price": 11.05, "market_cap": 19146460000.0})
    yield a
    a.db.close()


def llm_calls(app) -> int:
    return app.db._one("SELECT COUNT(*) AS n FROM llm_calls")["n"]


def fake_assess(app, monkeypatch, make):
    """把阶段 B（结构化修正）替换成给定的 JSON；make(user) → dict。其他调用照常走 mock。"""
    orig = app.llm.complete
    seen = []

    def complete(task, system, user, *a, **k):
        if task == "tracker":
            seen.append(user)
            return LLMResult(text="", data=make(user), model="fake", profile="mock", usage={})
        return orig(task, system, user, *a, **k)

    monkeypatch.setattr(app.llm, "complete", complete)
    return seen


def assessment(uid, changes=(), impact="material", status="intact", catalysts=()):
    return {"summary": "长协价上调，利润预测上修", "thesis_status": status, "stance": "bullish", "narrative": "说明",
            "events": [{"id": short_id(uid), "impact": impact, "note": "影响利润"}], "changes": list(changes),
            "catalysts": list(catalysts)}


# ---------------------------------------------------------------- 基线

def test_add_requires_dossier_and_builds_baseline_without_llm(tapp):
    c = tapp.watchlist.get("dongyue")
    with pytest.raises(tracker.TrackerError, match="还没有深度报告"):
        tracker.add(tapp, c)
    save_dossier(tapp)
    msg = tracker.add(tapp, c)
    assert "开始追踪" in msg and llm_calls(tapp) == 0
    rev = tapp.db.latest_revision("dongyue")
    assert rev["kind"] == "baseline" and rev["price"] == 11.05
    expected = 0.25 * (21.88 + 25.12) / 2 + 0.5 * (13.79 + 17.56) / 2 + 0.25 * (9.12 + 10.55) / 2
    assert rev["target"] == pytest.approx(expected) and rev["base"] == pytest.approx((13.79 + 17.56) / 2)
    assert "恢复追踪" in tracker.add(tapp, c)
    assert "已停止追踪" in tracker.remove(tapp, c) and tracker.run(tapp, [c]).msgs == ["没有追踪中的公司（stock target add <公司>）"]
    s = tracker.status(tapp, c)
    assert s["live_target"] == pytest.approx(expected, rel=1e-3) and s["upside"] > 0


def test_check_without_evidence_skips_llm(tapp):
    c = tapp.watchlist.get("dongyue")
    save_dossier(tapp)
    tracker.add(tapp, c)
    r = tracker.check(tapp, c)
    assert r.msgs == ["东岳集团：没有新依据，估值维持"] and llm_calls(tapp) == 0
    assert tapp.db.tracker("dongyue")["checked_at"]


# ---------------------------------------------------------------- 修正

def test_revision_applies_validated_changes(tapp, monkeypatch):
    c = tapp.watchlist.get("dongyue")
    save_dossier(tapp)
    tracker.add(tapp, c)
    uid = seed_event(tapp, "Q4 R32 长协价 68,400 元/吨，环比上调 5.1%")
    sid = short_id(uid)
    seen = fake_assess(tapp, monkeypatch, lambda user: assessment(uid, changes=[
        {"path": "all.driver", "op": "mul", "operand": 1.05, "reason": "长协价上调带动利润", "evidence": [sid], "derivation": "5.1% × 利润弹性"},
        {"path": "prob.bull", "op": "set", "operand": 0.35, "reason": "长协兑现", "evidence": [f"id={sid}"], "derivation": ""},
        {"path": "g.net_cash", "op": "set", "operand": 99.9, "reason": "凭空的数字", "evidence": [sid], "derivation": ""},
        {"path": "base.multiple", "op": "mul", "operand": 1.1, "reason": "没有依据", "evidence": ["XYZ"], "derivation": ""},
        {"path": "g.shares", "op": "mul", "operand": 1.02, "reason": "配售", "evidence": [sid], "derivation": ""},
    ], catalysts=[{"ref": "C1", "status": "pending", "note": ""}]))
    before = tapp.db.latest_revision("dongyue")
    r = tracker.check(tapp, c)
    assert f"id={sid}" in seen[0] and "R32 现货价" not in seen[0]  # 阶段 B 只给依据与笔记；跟踪卡给了阶段 A
    rev = tapp.db.latest_revision("dongyue")
    assert rev["kind"] == "revision" and "修正" in r.msgs[0]
    labels = {ch["path"] for ch in rev["changes"] if ch["source"] == "模型"}
    assert labels == {"bull.driver", "base.driver", "bear.driver", "prob.bull"}
    assert rev["base"] > before["base"] and rev["target"] > before["target"]
    why = " ".join(x["why"] for x in rev["rejected"])
    assert "找不到出处" in why and "没有引用本次提供的依据编号" in why and "只能 set 或 add" in why
    model = rev["model"]
    assert sum(s["probability"] for s in model["scenarios"]) == pytest.approx(1)  # 概率归一化
    assert rev["events"][0]["impact"] == "material" and tapp.db.refs("dongyue")[f"item:{uid}"]["revision_id"] == rev["id"]
    n = llm_calls(tapp)
    assert tracker.check(tapp, c).msgs == ["东岳集团：没有新依据，估值维持"] and llm_calls(tapp) == n  # 已处理的不重复


def test_fact_change_with_source_is_applied(tapp, monkeypatch):
    c = tapp.watchlist.get("dongyue")
    save_dossier(tapp)
    tracker.add(tapp, c)
    uid = seed_event(tapp, "中期报告：净现金 58.2 亿元，派息后减少")
    fake_assess(tapp, monkeypatch, lambda user: assessment(uid, changes=[
        {"path": "g.net_cash", "op": "set", "operand": 58.2, "reason": "中报净现金", "evidence": [short_id(uid)], "derivation": "公告"}]))
    tracker.check(tapp, c)
    rev = tapp.db.latest_revision("dongyue")
    assert rev["model"]["net_cash"] == 58.2 and not rev["rejected"]


def test_percent_probabilities_and_tool_sourced_facts(tapp, monkeypatch):
    c = tapp.watchlist.get("dongyue")
    save_dossier(tapp)
    tracker.add(tapp, c)
    uid = seed_event(tapp, "9 月交付不及指引下限，触发两条红旗")
    sid = short_id(uid)
    orig = tapp.llm.run_agent

    def agent(task, system, user, tools, call_tool, **k):  # 修正 agent 用工具查到一笔新的现金流出
        call_tool("calculate", {"code": "60.37 - 5.37"})
        return orig(task, system, user, tools, call_tool, **k)

    monkeypatch.setattr(tapp.llm, "run_agent", agent)
    fake_assess(tapp, monkeypatch, lambda user: assessment(uid, changes=[
        {"path": "prob.bull", "op": "set", "operand": 15, "reason": "红旗", "evidence": [sid], "derivation": ""},
        {"path": "prob.bear", "op": "set", "operand": 55, "reason": "红旗", "evidence": [sid], "derivation": ""},
        {"path": "g.net_cash", "op": "set", "operand": 55.0, "reason": "回购完成", "evidence": ["T1"], "derivation": "60.37−5.37"},
        {"path": "base.multiple", "op": "mul", "operand": 0.9, "reason": "只有工具结果", "evidence": ["T1"], "derivation": ""},
    ]))
    tracker.check(tapp, c)
    rev = tapp.db.latest_revision("dongyue")
    applied = {ch["path"]: ch for ch in rev["changes"] if ch["source"] == "模型"}
    assert applied["prob.bull"]["new"] == 0.15 and applied["prob.bear"]["new"] == 0.55  # 百分数按比例理解
    assert applied["g.net_cash"]["new"] == 55.0                                      # 事实类：工具结果即可作为依据
    assert [r["why"] for r in rev["rejected"]] == ["没有引用本次提供的依据编号"]            # 判断类必须由本次依据触发


def test_large_fact_change_needs_confirmation(tapp, monkeypatch):
    c = tapp.watchlist.get("dongyue")
    save_dossier(tapp)
    tracker.add(tapp, c)
    uid = seed_event(tapp, "中期报告刊发：现金类资产 404.6 亿元，借款 197.59 亿元，净额 207.01 亿元")
    fake_assess(tapp, monkeypatch, lambda user: {**assessment(uid, changes=[
        {"path": "g.net_cash", "op": "set", "operand": 207.01, "reason": "统一为报表口径", "evidence": [short_id(uid)], "derivation": ""}]),
        "questions": [{"topic": "净现金口径", "detail": "报告正文与估值矩阵口径不一致"}]})
    tracker.check(tapp, c)
    rev = tapp.db.latest_revision("dongyue")
    assert rev["model"]["net_cash"] == 60.37 and rev["kind"] == "review"  # 变动超过 25%：不自动应用
    pending = [i for i, r in enumerate(rev["rejected"]) if r.get("confirm")]
    assert pending and any("净现金口径" in f for f in rev["flags"]) and any("待你确认" in f for f in rev["flags"])
    done = tracker.confirm_change(tapp, c, rev["id"], pending[0])
    assert done["model"]["net_cash"] == 207.01 and done["changes"][0]["source"] == "使用者确认" and done["target"] > rev["target"]
    with pytest.raises(tracker.TrackerError, match="已经采纳过"):
        tracker.confirm_change(tapp, c, rev["id"], pending[0])


def test_mock_smoke_revision_does_not_crash(tapp):
    c = tapp.watchlist.get("dongyue")
    save_dossier(tapp)
    tracker.add(tapp, c)
    seed_event(tapp, "签订重大合同", materiality=4)
    r = tracker.check(tapp, c)
    rev = tapp.db.latest_revision("dongyue")
    assert rev["kind"] in ("revision", "review") and rev["notes_md"] and r.msgs
    assert any(x["tool"] == "calculate" for x in rev["tool_log"])  # mock agent 调用过白名单内的工具


def test_late_triaged_and_overflow(tapp, monkeypatch):
    c = tapp.watchlist.get("dongyue")
    save_dossier(tapp)
    tracker.add(tapp, c)
    old = seed_event(tapp, "十天前发布、刚补抓到的公告", published=utcnow() - timedelta(days=10))
    fresh = [seed_event(tapp, f"新公告 {i}", materiality=3) for i in range(2)]
    ignored = seed_event(tapp, "不重要的消息", materiality=2)
    tapp.settings.tracker.max_events = 2
    fake_assess(tapp, monkeypatch, lambda user: {"summary": "维持", "thesis_status": "intact", "stance": "bullish",
                                                 "narrative": "", "events": [], "changes": [], "catalysts": []})
    progress = []
    tracker.check(tapp, c, progress=progress.append)
    assert any("2 条新依据（另有 1 条留到下次）" in p for p in progress)
    assert tapp.db.latest_revision("dongyue")["kind"] == "review"
    refs = tapp.db.refs("dongyue", "item:")
    assert f"item:{old}" in refs and refs[f"item:{old}"]["impact"] == "unassessed"  # 按重要性优先、分级时间取窗口
    assert len(refs) == 2 and f"item:{ignored}" not in refs
    tracker.check(tapp, c)
    assert set(tapp.db.refs("dongyue", "item:")) == {f"item:{old}", *(f"item:{u}" for u in fresh)}


def test_trigger_escalation_is_evidence(tapp, monkeypatch):
    c = tapp.watchlist.get("dongyue")
    save_dossier(tapp, triggers=[{"description": "R32 跌破 6 万元/吨", "kind": "fundamental", "action": "reduce"}])
    tracker.add(tapp, c)
    tid = tapp.db.triggers("dongyue")[0]["id"]
    tapp.db.update_trigger(tid, "warning", "隆众：R32 报 61,000")
    ev, _ = tracker.collect(tapp, c, tapp.db.latest_dossier("dongyue"), tapp.db.latest_revision("dongyue"))
    assert [e.code for e in ev] == [f"K{tid}"]
    fake_assess(tapp, monkeypatch, lambda user: {"summary": "维持", "thesis_status": "weakened", "stance": "neutral",
                                                 "narrative": "", "events": [{"id": f"K{tid}", "impact": "minor", "note": ""}],
                                                 "changes": [], "catalysts": []})
    r = tracker.check(tapp, c)
    assert r.notable  # 论点削弱：值得推送
    ev, _ = tracker.collect(tapp, c, tapp.db.latest_dossier("dongyue"), tapp.db.latest_revision("dongyue"))
    assert ev == []  # 状态快照已更新，同一状态不再作为新依据


def test_new_dossier_rebases_and_supersedes(tapp):
    c = tapp.watchlist.get("dongyue")
    save_dossier(tapp, created_at=iso(utcnow() - timedelta(days=5)))
    tracker.add(tapp, c)
    uid = seed_event(tapp, "旧基线下未处理的公告")
    tapp.db._exec("UPDATE items SET triaged_at=? WHERE uid=?", (iso(utcnow() - timedelta(days=3)), uid))
    save_dossier(tapp)  # 新报告
    r = tracker.check(tapp, c, use_llm=False)
    assert "重建估值基线" in r.msgs[0]
    assert tapp.db.refs("dongyue")[f"item:{uid}"]["impact"] == "superseded"
    assert tapp.db.tracker("dongyue")["dossier_id"] == tapp.db.latest_dossier("dongyue")["id"]


def test_redo_queued_once_within_cooldown(tapp, monkeypatch):
    c = tapp.watchlist.get("dongyue")
    save_dossier(tapp)
    tracker.add(tapp, c)
    uid = seed_event(tapp, "2027 年配额正式稿大幅削减东岳 R32 配额", materiality=5, direction="negative")
    fake_assess(tapp, monkeypatch, lambda user: assessment(re_uid[0], impact="material", status="weakened"))
    re_uid = [uid]
    r = tracker.check(tapp, c)
    jobs = tapp.db.jobs()
    assert len(jobs) == 1 and jobs[0]["task"] == "dossier" and jobs[0]["argv"] == [["dossier", "dongyue"]]
    assert "已排队重跑深度报告" in "".join(r.msgs) and r.notable
    re_uid[0] = seed_event(tapp, "配额再次削减", materiality=5, direction="negative")
    r = tracker.check(tapp, c)
    assert len(tapp.db.jobs()) == 1 and "不重复排队" in "".join(r.msgs)
    assert tracker.status(tapp, c)["redo"]["id"] == jobs[0]["id"]


def test_llm_failure_links_nothing(tapp, monkeypatch):
    from stockagent.llm import LLMError

    c = tapp.watchlist.get("dongyue")
    save_dossier(tapp)
    tracker.add(tapp, c)
    uid = seed_event(tapp, "公告")

    def boom(*a, **k):
        raise LLMError("网关不可用", retryable=True)

    monkeypatch.setattr(tapp.llm, "run_agent", boom)
    r = tracker.check(tapp, c)
    assert r.msgs[0].startswith("✗") and f"item:{uid}" not in tapp.db.refs("dongyue")


# ---------------------------------------------------------------- 工具与集成

def test_toolbox_allowlist_and_web_search_pause(tapp):
    c = tapp.watchlist.get("dongyue")
    box = ToolBox(tapp, c, allow=["calculate"])
    assert [s["name"] for s in box.specs()] == ["calculate"]
    assert box.call("web_search", {"query": "东岳"}).startswith("错误：没有名为 web_search 的工具")
    assert box.call("calculate", {"code": "1 + 1"}) == "结果：2"
    allow, why = tracker._allowed_tools(tapp)
    assert "web_search" in allow and not why
    with tapp.lock("dossier") as ok:
        assert ok
        allow, why = tracker._allowed_tools(tapp)
        assert "web_search" not in allow and "深度报告正在运行" in why


def test_brief_shows_target_but_not_to_llm(tapp, monkeypatch):
    c = tapp.watchlist.get("dongyue")
    save_dossier(tapp)
    tracker.add(tapp, c)
    seen = []
    orig = tapp.llm.complete
    monkeypatch.setattr(tapp.llm, "complete", lambda task, system, user, *a, **k: seen.append(user) or orig(task, system, user, *a, **k))
    md_path, *_ = brief.build(tapp, [c])
    text = md_path.read_text(encoding="utf-8")
    assert tracker.TARGET_LABEL in text and "空间" in text
    assert seen and not any(tracker.TARGET_LABEL in u for u in seen)


def test_monitor_checks_late_triaged_items(tapp, monkeypatch):
    c = tapp.watchlist.get("dongyue")
    save_dossier(tapp, triggers=[{"description": "失去第一大客户", "kind": "event", "action": "reduce"}])
    tapp.db.kv_set("monitor_checked:dongyue", iso(utcnow() - timedelta(hours=1)))
    seed_event(tapp, "两天前发布、刚补抓到：第一大客户终止合作", published=utcnow() - timedelta(days=2))
    seen = []
    orig = tapp.llm.complete
    monkeypatch.setattr(tapp.llm, "complete", lambda task, system, user, *a, **k: seen.append(user) or orig(task, system, user, *a, **k))
    monitor.check_events(tapp, c)
    assert seen and "第一大客户终止合作" in seen[0]


def test_locks_and_run_step_order(tapp, monkeypatch, capsys, project):
    monkeypatch.setattr(cli.appmod, "fetch", lambda *a, **k: [])
    monkeypatch.setattr(cli, "_ensure_reports", lambda *a, **k: [])
    monkeypatch.setattr(cli, "_load", lambda args: tapp)
    save_dossier(tapp)
    tracker.add(tapp, tapp.watchlist.get("dongyue"))
    cli.main(["-c", str(project / "config.yaml"), "run", "--no-llm"])
    out = capsys.readouterr().out
    assert out.index("5/6 标的追踪") < out.index("6/6") and "没有新依据" in out
    with tapp.lock("pipeline"):
        cli.main(["-c", str(project / "config.yaml"), "run", "--no-llm"])
        assert cli.PIPELINE_BUSY in capsys.readouterr().out
    with tapp.lock("dossier"):
        with pytest.raises(SystemExit, match="已有深度报告在生成"):
            cli.main(["-c", str(project / "config.yaml"), "dossier", "dongyue"])


def test_ima_documents_are_not_separate_evidence(tapp, tmp_path):
    """ima 原文入库时对应的事件已写明资料编号，不再作为「新入库资料」重复出现；手动添加的资料照常。"""
    from stockagent.docs import store_document

    c = tapp.watchlist.get("dongyue")
    save_dossier(tapp, created_at=iso(utcnow() - timedelta(days=1)))
    tracker.add(tapp, c)
    for name, source in (("ima.txt", "ima"), ("manual.txt", "manual")):
        f = tmp_path / name
        f.write_text(f"{source} 研报正文", encoding="utf-8")
        store_document(tapp.db, tapp.data_dir, "dongyue", f, doc_type="research", period=None, title=name, source=source)
    ev, _ = tracker.collect(tapp, c, tapp.db.latest_dossier("dongyue"), tapp.db.latest_revision("dongyue"))
    assert [e.text.split("《")[1].split("》")[0] for e in ev] == ["manual.txt"]
    assert {"ima_search", "ima_read"} <= set(tracker.TOOLS)
