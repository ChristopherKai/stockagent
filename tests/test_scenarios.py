"""程序定价的 12 个月情景：模型在矩阵里选点，价格由代码算；跟踪卡与追踪基线直接取用。"""

from __future__ import annotations

import json
import re

import pytest

from stockagent import dossier, scenarios, valuation as V
from stockagent.llm import LLMError, LLMResult
from stockagent.tools import ToolBox, matrix_price, matrix_table

ARGS = {"driver_label": "2027E归母净利(亿CNY)", "driver_values": [80, 90, 100, 110, 120], "multiple_label": "PE(×)",
        "multiples": [6, 8, 10, 12], "shares": 54.71, "net_cash": 0.0, "fx": 1.17, "basis": "equity"}
ENTRY = {"kind": "matrix", "index": 0, "args": ARGS, "basis": "equity"}


def _pts(**over):
    pts = [{"name": "bull", "driver": 115, "multiple_low": 10, "multiple_high": 12, "probability": 0.25, "drivers": "放量"},
           {"name": "base", "driver": 100, "multiple_low": 8, "multiple_high": 10, "probability": 0.5, "drivers": "稳健"},
           {"name": "bear", "driver": 85, "multiple_low": 6, "multiple_high": 8, "probability": 0.25, "drivers": "承压"}]
    for p in pts:
        p.update(over.get(p["name"], {}))
    return {"matrix": 0, "scenarios": pts}


def test_build_spec_prices_come_from_matrix_formula():
    spec = scenarios.build_spec([ENTRY], _pts())
    scen = {s["name"]: s for s in scenarios.compute(spec)}
    assert scen["base"]["price_low"] == pytest.approx(matrix_price(100, 8, 54.71, 0, 1.17, "equity"), abs=1e-3)
    assert scen["base"]["price_high"] == pytest.approx(matrix_price(100, 10, 54.71, 0, 1.17, "equity"), abs=1e-3)
    assert scen["bull"]["price_low"] > scen["base"]["price_high"] > scen["bear"]["price_low"]
    assert [p["name"] for p in spec["points"]] == ["bull", "base", "bear"] and spec["call"] == 0


def test_build_spec_normalizes_probabilities():
    spec = scenarios.build_spec([ENTRY], _pts(bull={"probability": 50}, base={"probability": 100}, bear={"probability": 50}))
    assert [p["probability"] for p in spec["points"]] == pytest.approx([0.25, 0.5, 0.25])


@pytest.mark.parametrize("bad,msg", [
    ({"matrix": 7}, "不在备选清单"),
    ({"scenarios": _pts()["scenarios"][:2]}, "bull / base / bear"),
    (_pts(bull={"driver": 400}), "超出所选估值"),                                       # 驱动远在矩阵之外
    (_pts(bear={"multiple_low": 9, "multiple_high": 7}), "不合理"),                       # 倍数区间倒置
    (_pts(bull={"driver": 85, "multiple_low": 6, "multiple_high": 8}, bear={"driver": 115, "multiple_low": 10, "multiple_high": 12}),
     "顺序不对"),                                                                         # 熊市价格高于牛市
])
def test_build_spec_rejects_bad_choices(bad, msg):
    data = {**_pts(), **bad}
    with pytest.raises(ValueError, match=msg):
        scenarios.build_spec([ENTRY], data)


def test_table_text_and_report_mismatches():
    spec = scenarios.build_spec([ENTRY], _pts())
    text = scenarios.table_text(spec, 15.0, "HKD")
    assert "程序按估值公式计算" in text and "概率加权价格" in text and "工具调用 #1" in text
    scen = scenarios.compute(spec)
    good = "## § 12 个月情景\n" + "\n".join(f"{s['name']}：{s['price_low']:.2f}–{s['price_high']:.2f}" for s in scen) + "\n## § 下一章\n"
    assert scenarios.report_mismatches(good, spec) == []
    changed = good.replace(f"{scen[1]['price_low']:.2f}", "99.99")
    assert scenarios.report_mismatches(changed, spec) == [f"基准下沿 {scen[1]['price_low']:.2f}"]
    assert scenarios.report_mismatches("没有这个章节", spec) == []


def _dossier_with_spec(app, spec, log=None):
    card = {"thesis": "论点", "stance": "neutral", "price_listing": "HK:01234", "scenarios": scenarios.compute(spec),
            "valuation_spec": spec}
    app.db.save_dossier("acme", "mock", "# 报告", card, {}, log or [], [], [], {})
    return app.db.latest_dossier("acme")


def test_baseline_rebuilds_exactly_from_spec_without_tool_log(app):
    """跟踪卡带 valuation_spec：不看工具日志，驱动与倍数就是当时定下的点，情景价格与卡片区间中点完全一致。"""
    spec = scenarios.build_spec([ENTRY], _pts())
    d = _dossier_with_spec(app, spec)  # 工具日志为空：旧办法会退化成区间模型
    model, flags = V.build_baseline(app, app.watchlist.get("acme"), d)
    assert model.method == "matrix" and "程序定价情景" in model.source
    for s in scenarios.compute(spec):
        assert model.price(model.scenario(s["name"])) == pytest.approx((s["price_low"] + s["price_high"]) / 2, abs=1e-3)
    assert model.scenario("base").driver == 100 and model.scenario("base").multiple == pytest.approx(9)
    assert model.basis == "equity" and model.shares == pytest.approx(54.71)
    assert sum(s.probability for s in model.scenarios) == pytest.approx(1)


def test_baseline_falls_back_when_spec_is_broken(app):
    spec = scenarios.build_spec([ENTRY], _pts())
    d = _dossier_with_spec(app, spec)
    card = d["card"]
    card["valuation_spec"] = {"basis": "ev"}  # 残缺
    app.db.save_dossier("acme", "mock", "# 报告", card, {}, [], [], [], {})
    model, flags = V.build_baseline(app, app.watchlist.get("acme"), app.db.latest_dossier("acme"))
    assert model.method == "range" and any("程序定价的情景参数残缺" in f for f in flags)


def _matrix_log():
    return {"tool": "valuation_matrix", "args": ARGS, "section": "市场与估值",
            "meta": {"basis": "equity", "driver_values": ARGS["driver_values"], "multiples": ARGS["multiples"]},
            "result": matrix_table(ARGS["driver_label"], ARGS["driver_values"], ARGS["multiple_label"], ARGS["multiples"],
                                   54.71, 0, 1.17, basis="equity")}


def test_generate_prices_scenarios_in_code(app, monkeypatch):
    """端到端：研究里有矩阵调用 → 情景由程序定价，写进笔记、跟踪卡与 valuation_spec，追踪基线精确重建。"""
    monkeypatch.setattr(dossier, "_find_chrome", lambda: None)
    real_research, real_complete = dossier.research, app.llm.complete

    def research(app_, company, key, title, questions, market):
        k, t, text, log = real_research(app_, company, key, title, questions, market)
        return k, t, text, log + ([_matrix_log()] if key == "valuation" else [])

    def complete(task, system, user, *a, schema=None, validate=None, **k):
        if schema is scenarios.SCHEMA:
            idx = int(re.search(r"备选估值：\n\[(\d+)\]", user).group(1))  # 矩阵在工具日志里的位置（前面还有 calculate 等调用）
            data = validate({**_pts(), "matrix": idx})
            return LLMResult(text="", data=data, model="m", profile="p", usage={})
        return real_complete(task, system, user, *a, schema=schema, validate=validate, **k)

    monkeypatch.setattr(dossier, "research", research)
    monkeypatch.setattr(app.llm, "complete", complete)
    msgs = []
    r = dossier.generate(app, app.watchlist.get("acme"), progress=msgs.append)
    assert any("情景已由程序按敏感性矩阵定价" in m for m in msgs)
    d = app.db.latest_dossier("acme")
    notes = json.loads(d["notes_json"])
    assert "scenarios" in notes and "程序按估值公式计算" in notes["scenarios"]["text"]
    spec = d["card"]["valuation_spec"]
    assert d["card"]["scenarios"] == scenarios.compute(spec) and r["card"]["valuation_spec"]["call"] == spec["call"]
    assert "情景价格由程序按估值公式计算" in r["html_path"].read_text(encoding="utf-8")
    model, _ = V.build_baseline(app, app.watchlist.get("acme"), d)
    assert model.method == "matrix" and "程序定价情景" in model.source


def test_generate_without_matrix_keeps_old_flow(app, monkeypatch):
    monkeypatch.setattr(dossier, "_find_chrome", lambda: None)
    msgs = []
    dossier.generate(app, app.watchlist.get("acme"), progress=msgs.append)
    d = app.db.latest_dossier("acme")
    assert "valuation_spec" not in d["card"] and "scenarios" not in json.loads(d["notes_json"])
    assert any("没有可用的估值矩阵或分部加总" in m for m in msgs)


def test_propose_survives_llm_failure(app, monkeypatch):
    def boom(*a, **k):
        raise LLMError("down")
    monkeypatch.setattr(app.llm, "complete", boom)
    spec, why = scenarios.propose(app, app.watchlist.get("acme"), "笔记", [_matrix_log()], "行情", "HKD")
    assert spec is None and "程序定价情景失败" in why


# ---------------------------------------------------------------- 分部加总（SOTP）

from stockagent.tools import sotp_part_value, sotp_price, sotp_value  # noqa: E402

# 两个分部：主业按 PE 估值（驱动 = 分部利润），子公司按持股折价估值；股本 10 亿股，净现金 20，汇率 1.2，控股折价 10%
SEG = [("主业", 1.0, 0.0), ("子公司", 0.6, 0.2)]


def _sotp_call(main, sub, discount=0.2, **over):
    """一次 sotp 工具调用（日志条目）：main / sub = (driver, multiple)；discount = 子公司折价。"""
    a = {"parts": [{"name": "主业", "driver": main[0], "multiple": main[1], "stake": 1.0, "method": "PE"},
                   {"name": "子公司", "driver": sub[0], "multiple": sub[1], "stake": 0.6, "discount": discount, "method": "可比公司 PB"}],
         "shares": 10, "net_cash": 20, "fx": 1.2, "holdco_discount": 0.1, **over}
    return {"tool": "sotp", "args": a, "result": "| 部分 |"}


def _sotp_log():  # 熊 / 基 / 牛 各算一次，中间夹着无关的调用；折价与控股折价随情景变（真实报告里就是这样：熊市折价更狠）
    return [{"tool": "calculate", "args": {}, "result": "1"}, _sotp_call((8, 6), (50, 1.0), 0.3, holdco_discount=0.15),
            _sotp_call((10, 8), (60, 1.2), 0.2, holdco_discount=0.1),
            {"tool": "web_search", "args": {}, "result": "x"}, _sotp_call((12, 10), (70, 1.4), 0.1, holdco_discount=0.05)]


def _sotp_menu(app, log=None):
    return scenarios.sotp_menu(app, app.watchlist.get("acme"), log or _sotp_log())


def _sotp_pts():
    def parts(m, mlo, mhi, s, slo, shi, disc):
        return [{"name": "主业", "driver": m, "multiple_low": mlo, "multiple_high": mhi, "discount": 0.0},
                {"name": "子公司", "driver": s, "multiple_low": slo, "multiple_high": shi, "discount": disc}]
    return {"matrix": 4, "scenarios": [
        {"name": "bull", "driver": 0, "multiple_low": 0, "multiple_high": 0, "probability": 0.25, "drivers": "放量", "holdco_discount": 0.05,
         "parts": parts(12, 9, 10, 70, 1.3, 1.4, 0.1)},
        {"name": "base", "driver": 0, "multiple_low": 0, "multiple_high": 0, "probability": 0.5, "drivers": "稳健", "holdco_discount": 0.1,
         "parts": parts(10, 7, 8, 60, 1.1, 1.2, 0.2)},
        {"name": "bear", "driver": 0, "multiple_low": 0, "multiple_high": 0, "probability": 0.25, "drivers": "承压", "holdco_discount": 0.15,
         "parts": parts(8, 6, 7, 50, 1.0, 1.1, 0.3)}]}


def test_sotp_tool_accepts_driver_times_multiple():
    assert sotp_part_value({"name": "a", "value": 100}) == 100
    assert sotp_part_value({"name": "a", "driver": 8, "multiple": 12.5}) == 100
    assert sotp_part_value({"name": "a", "value": 100.4, "driver": 8, "multiple": 12.5}) == 100  # 1% 内视为一致，以乘积为准
    with pytest.raises(ValueError, match="不一致"):
        sotp_part_value({"name": "a", "value": 120, "driver": 8, "multiple": 12.5})
    with pytest.raises(ValueError, match="需要 value"):
        sotp_part_value({"name": "a", "driver": 8})
    rows, equity, per_share = sotp_value([{"name": "主业", "driver": 10, "multiple": 8}, {"name": "子公司", "driver": 60, "multiple": 1.2, "stake": 0.6, "discount": 0.2}],
                                         shares=10, net_cash=20, fx=1.2, holdco_discount=0.1)
    assert rows[1]["value"] == pytest.approx(72) and per_share == pytest.approx(sotp_price([(80, 1, 0), (72, 0.6, 0.2)], 10, 20, 1.2, 0.1))
    assert per_share == pytest.approx((80 + 72 * 0.6 * 0.8 + 20) * 0.9 / 10 * 1.2)


def test_sotp_menu_groups_calls_and_collects_ranges(app):
    menu = _sotp_menu(app)
    assert len(menu) == 1 and menu[0]["kind"] == "sotp" and menu[0]["index"] == 4 and menu[0]["calls"] == [1, 2, 4]
    seg = {g["name"]: g for g in menu[0]["segments"]}
    assert seg["主业"]["drivers"] == [8, 10, 12] and seg["主业"]["multiples"] == [6, 8, 10]
    assert seg["子公司"]["stake"] == 0.6 and seg["子公司"]["discounts"] == [0.3, 0.2, 0.1] and menu[0]["holdcos"] == [0.15, 0.1, 0.05]
    text = scenarios.menu_text(menu)  # 折价不同的三次调用仍是同一个分部加总，折价的取值合起来作为范围
    assert text.startswith("[4] 分部加总 SOTP（工具调用 #2、#3、#5）") and "控股公司折价取值 5%, 10%, 15%" in text and "折价取值 10%, 20%, 30%" in text


def test_sotp_menu_skips_bad_and_splits_by_structure(app):
    log = _sotp_log() + [_sotp_call((10, 8), (60, 1.2), net_cash=99),                       # 净现金不同 → 另一组
                         {**_sotp_call((10, 8), (60, 1.2)), "result": "错误：shares 必须为正"},  # 工具报错 → 跳过
                         {"tool": "sotp", "args": {"parts": [{"name": "x"}], "shares": 10}, "result": "ok"}]  # 残缺 → 跳过
    menu = _sotp_menu(app, log)
    assert [m["calls"] for m in menu] == [[1, 2, 4], [5]]
    # 只给 value 的分部：驱动 = 价值，倍数 = 1
    old = {"tool": "sotp", "args": {"parts": [{"name": "A", "value": 100}, {"name": "B", "value": 50, "stake": 0.5}], "shares": 10}, "result": "ok"}
    seg = _sotp_menu(app, [old])[0]["segments"]
    assert seg[0]["drivers"] == [100] and seg[0]["multiples"] == [1.0] and seg[1]["stake"] == 0.5
    assert scenarios.sotp_menu(app, app.watchlist.get("acme"), []) == []


def test_sotp_spec_prices_follow_the_formula(app):
    menu = _sotp_menu(app)
    spec = scenarios.build_spec(menu, _sotp_pts())
    assert spec["kind"] == "sotp" and [p["name"] for p in spec["points"]] == ["bull", "base", "bear"]
    base = next(s for s in scenarios.compute(spec) if s["name"] == "base")
    lo = sotp_price([(10 * 7, 1.0, 0.0), (60 * 1.1, 0.6, 0.2)], 10, 20, 1.2, 0.1)
    hi = sotp_price([(10 * 8, 1.0, 0.0), (60 * 1.2, 0.6, 0.2)], 10, 20, 1.2, 0.1)
    assert base["price_low"] == pytest.approx(lo, abs=1e-3) and base["price_high"] == pytest.approx(hi, abs=1e-3)
    assert lo == pytest.approx((70 + 66 * 0.6 * 0.8 + 20) * 0.9 / 10 * 1.2)  # 手算：Σ 驱动×倍数×持股×(1−折价) + 净现金，再扣控股折价
    assert sum(p["probability"] for p in spec["points"]) == pytest.approx(1)
    bear = next(s for s in scenarios.compute(spec) if s["name"] == "bear")  # 熊市：子公司折价 30%、控股折价 15%
    assert bear["price_low"] == pytest.approx((8 * 6 + 50 * 1.0 * 0.6 * 0.7 + 20) * 0.85 / 10 * 1.2, abs=1e-3)


@pytest.mark.parametrize("edit,msg", [
    (lambda d: d["scenarios"][0]["parts"].pop(), "分部要与清单一致"),                                   # 缺分部
    (lambda d: d["scenarios"][0]["parts"][0].update(name="别的业务"), "分部要与清单一致"),               # 分部名不对
    (lambda d: d["scenarios"][0]["parts"][0].update(driver=40), "超出所选估值的取值范围"),               # 驱动远超各次调用的范围
    (lambda d: d["scenarios"][1]["parts"][1].update(multiple_low=5, multiple_high=6), "超出所选估值的取值范围"),
    (lambda d: d["scenarios"][0]["parts"][1].update(driver=50, multiple_low=1.0, multiple_high=1.1, discount=0.3)
     or d["scenarios"][0]["parts"][0].update(driver=8, multiple_low=6, multiple_high=7), "顺序不对"),   # 牛市价格低于基准
    (lambda d: d["scenarios"][1]["parts"][1].update(discount=0.6), "折价 60% 超出所选估值的取值范围"),    # 折价超出各次调用的范围
    (lambda d: d["scenarios"][2].update(holdco_discount=0.5), "控股公司的折价 50% 超出"),               # 控股折价同理
    (lambda d: d["scenarios"][2].update(holdco_discount=1.2), r"应在 \[0, 1\) 内"),
])
def test_sotp_spec_rejects_bad_choices(app, edit, msg):
    data = _sotp_pts()
    edit(data)
    with pytest.raises(ValueError, match=msg):
        scenarios.build_spec(_sotp_menu(app), data)


def test_sotp_table_text_and_mismatches(app):
    spec = scenarios.build_spec(_sotp_menu(app), _sotp_pts())
    text = scenarios.table_text(spec, 15.0, "HKD")
    assert "分部加总" in text and "主业 10 × 7–8×" in text and "子公司 60 × 1.1–1.2×（持股 60%、折价 20%）" in text
    assert "控股公司折价 10%" in text and "控股公司折价 5%" in text and "控股公司折价 15%" in text  # 各情景各自的控股折价
    assert scenarios.describe_spec(spec) == "分部加总（SOTP）"
    scen = scenarios.compute(spec)
    good = "## § 12 个月情景\n" + "\n".join(f"{s['price_low']:.2f}–{s['price_high']:.2f}" for s in scen) + "\n## § 下一章\n"
    assert scenarios.report_mismatches(good, spec) == []
    assert scenarios.report_mismatches(good.replace(f"{scen[2]['price_high']:.2f}", "1.00"), spec) == [f"熊市上沿 {scen[2]['price_high']:.2f}"]


def _sotp_model(app):
    spec = scenarios.build_spec(_sotp_menu(app), _sotp_pts())
    d = _dossier_with_spec(app, spec)
    return spec, V.build_baseline(app, app.watchlist.get("acme"), d)


def test_sotp_baseline_rebuilds_exactly(app):
    spec, (model, flags) = _sotp_model(app)
    assert model.method == "sotp" and "分部加总" in model.source and model.holdco_discount == pytest.approx(0.1)  # 默认值取基准情景
    assert [model.scenario(n).holdco_discount for n in ("bull", "base", "bear")] == pytest.approx([0.05, 0.1, 0.15])
    assert [model.scenario(n).segments[1].discount for n in ("bull", "base", "bear")] == pytest.approx([0.1, 0.2, 0.3])
    for s in scenarios.compute(spec):  # 每个情景的价格恰好等于卡片区间中点
        assert model.price(model.scenario(s["name"])) == pytest.approx((s["price_low"] + s["price_high"]) / 2, abs=1e-3)
    base = model.scenario("base")
    assert [(g.name, g.driver, g.multiple, g.stake, g.discount) for g in base.segments] == [
        ("主业", 10, 7.5, 1.0, 0.0), ("子公司", 60, pytest.approx(1.15), 0.6, 0.2)]
    assert model.target() == pytest.approx(sum(s.probability * model.price(s) for s in model.scenarios))
    assert V.load_model(model.model_dump_json()).price(model.scenario("bull")) == pytest.approx(model.price(model.scenario("bull")))  # 存取一圈不变
    text = V.describe(model, "expected", 15.0)
    assert "p1=主业 10.00 × 7.50" in text and "p2=子公司" in text and "控股公司折价 5%" in text and "控股公司折价 15%" in text


def test_sotp_model_paths_and_revisions(app):
    from stockagent import tracker

    _, (model, _) = _sotp_model(app)
    ps = V.paths(model)
    assert "bull.p1.driver" in ps and "all.p2.multiple" in ps and "g.net_cash" in ps and "bull.driver" not in ps and "prob.base" in ps
    assert V.path_label(model, "bull.p2.multiple") == "牛市·子公司倍数" and V.path_label(model, "all.p1.driver") == "全部情景·主业驱动指标"
    assert [p for p, _ in V.get_values(model, "all.p1.multiple")] == ["bull.p1.multiple", "base.p1.multiple", "bear.p1.multiple"]
    assert V.check_value(model, "base.p1.multiple", 0) == "倍数必须为正" and V.check_value(model, "base.p1.driver", 5) is None
    before = model.price(model.scenario("base"))
    changes = [{"path": "base.p1.multiple", "op": "mul", "operand": 1.1, "reason": "同业估值抬升", "evidence": ["id=abc"]},
               {"path": "all.p2.driver", "op": "set", "operand": 66, "reason": "子公司利润上修", "evidence": ["id=abc"], "derivation": "公告 66"},
               {"path": "bull.driver", "op": "set", "operand": 1, "reason": "x", "evidence": ["id=abc"]}]  # SOTP 没有情景级驱动
    new, applied, rejected = tracker.apply_changes(model, changes, {"abc": "公告：子公司利润上修至 66 亿元"}, {}, 0.5)
    assert [a["path"] for a in applied] == ["base.p1.multiple"] + [f"{n}.p2.driver" for n in ("bull", "base", "bear")]
    assert new.scenario("base").segments[0].multiple == pytest.approx(7.5 * 1.1) and new.scenario("base").segments[1].driver == 66
    assert new.price(new.scenario("base")) > before and "不存在的假设路径" in rejected[0]["why"]
    assert model.scenario("base").segments[0].multiple == 7.5  # 原模型不被改动（修改在副本上）
    live, _ = V.refresh_fx(app, model)  # 汇率刷新对 SOTP 模型同样适用（这里没有汇率数据，原样返回）
    assert live.method == "sotp"


def test_sotp_model_rejects_inconsistent_segments():
    seg = lambda *n: [V.Segment(name=x, driver=1, multiple=1) for x in n]  # noqa: E731
    pts = lambda a, b: [V.ScenarioPoint(name="bull", driver=0, multiple=1, probability=0.5, segments=seg(*a)),
                        V.ScenarioPoint(name="base", driver=0, multiple=1, probability=0.5, segments=seg(*b))]
    kw = dict(method="sotp", driver_label="d", multiple_label="m", shares=1, price_currency="HKD", listing="HK:01234")
    V.ValuationModel(**kw, scenarios=pts(["A", "B"], ["A", "B"]))
    with pytest.raises(ValueError, match="分部名称与顺序一致"):
        V.ValuationModel(**kw, scenarios=pts(["A", "B"], ["B", "A"]))
    with pytest.raises(ValueError, match="分部名称与顺序一致"):
        V.ValuationModel(**kw, scenarios=pts([], []))


def test_sotp_baseline_falls_back_without_segments_in_spec(app):
    spec = scenarios.build_spec(_sotp_menu(app), _sotp_pts())
    d = _dossier_with_spec(app, spec)
    card = d["card"]
    card["valuation_spec"] = {**spec, "segments": []}  # 分部清单丢了：精确重建不了，退回旧办法（工具日志为空，只剩区间模型）
    app.db.save_dossier("acme", "mock", "# 报告", card, {}, [], [], [], {})
    model, flags = V.build_baseline(app, app.watchlist.get("acme"), app.db.latest_dossier("acme"))
    assert model.method == "range" and any("参数残缺" in f for f in flags)


def test_generate_prices_sotp_scenarios(app, monkeypatch):
    """端到端：研究里调了三次 sotp → 选点、写进笔记与跟踪卡，追踪基线是 SOTP 模型。"""
    monkeypatch.setattr(dossier, "_find_chrome", lambda: None)
    real_research, real_complete = dossier.research, app.llm.complete
    seen = {}

    def research(app_, company, key, title, questions, market):
        k, t, text, log = real_research(app_, company, key, title, questions, market)
        return k, t, text, log + ([_sotp_call((8, 6), (50, 1.0), 0.3, holdco_discount=0.15), _sotp_call((10, 8), (60, 1.2), 0.2, holdco_discount=0.1),
                                   _sotp_call((12, 10), (70, 1.4), 0.1, holdco_discount=0.05)] if key == "valuation" else [])

    def complete(task, system, user, *a, schema=None, validate=None, **k):
        if schema is scenarios.SCHEMA:
            seen["menu"] = re.search(r"备选估值：\n\[(\d+)\] 分部加总", user)
            idx = int(seen["menu"].group(1))
            return LLMResult(text="", data=validate({**_sotp_pts(), "matrix": idx}), model="m", profile="p", usage={})
        return real_complete(task, system, user, *a, schema=schema, validate=validate, **k)

    monkeypatch.setattr(dossier, "research", research)
    monkeypatch.setattr(app.llm, "complete", complete)
    r = dossier.generate(app, app.watchlist.get("acme"), progress=lambda *_: None)
    assert seen["menu"]
    d = app.db.latest_dossier("acme")
    spec = d["card"]["valuation_spec"]
    assert spec["kind"] == "sotp" and d["card"]["scenarios"] == scenarios.compute(spec)
    assert "分部加总" in json.loads(d["notes_json"])["scenarios"]["text"]
    assert "情景价格由程序按估值公式计算（分部加总（SOTP））" in r["html_path"].read_text(encoding="utf-8")
    model, _ = V.build_baseline(app, app.watchlist.get("acme"), d)
    assert model.method == "sotp" and f"第 {spec['call'] + 1} 次工具调用 sotp" in model.source


def test_sotp_accepts_negative_driver_value_only_part_and_varying_structure(app):
    """真实报告里的写法：总部费用是负的分部、只给 value 的上市子公司股权（倍数固定为 1）、折价与控股折价随情景变。"""
    def call(main, hq, disc, holdco):
        return {"tool": "sotp", "result": "ok", "args": {"shares": 10, "net_cash": 20, "fx": 1.0, "holdco_discount": holdco, "parts": [
            {"name": "主业", "driver": main, "multiple": 8, "method": "8× 分部利润"},
            {"name": "上市子公司", "value": 100, "stake": 0.44, "discount": disc, "method": "市值"},
            {"name": "总部", "driver": hq, "multiple": 5}]}}
    menu = scenarios.sotp_menu(app, app.watchlist.get("acme"), [call(8, -4, 0.4, 0.2), call(10, -3, 0.3, 0.15), call(12, -2, 0.2, 0.1)])
    assert len(menu) == 1 and menu[0]["segments"][2]["drivers"] == [-4, -3, -2] and menu[0]["segments"][1]["multiples"] == [1.0, 1.0, 1.0]

    def pt(name, main, hq, disc, holdco, prob):
        return {"name": name, "driver": 0, "multiple_low": 0, "multiple_high": 0, "probability": prob, "drivers": name, "holdco_discount": holdco,
                "parts": [{"name": "主业", "driver": main, "multiple_low": 8, "multiple_high": 8, "discount": 0.0},
                          {"name": "上市子公司", "driver": 100, "multiple_low": 1, "multiple_high": 1, "discount": disc},
                          {"name": "总部", "driver": hq, "multiple_low": 5, "multiple_high": 5, "discount": 0.0}]}
    data = {"matrix": menu[0]["index"], "scenarios": [pt("bull", 12, -2, 0.2, 0.1, 0.25), pt("base", 10, -3, 0.3, 0.15, 0.5), pt("bear", 8, -4, 0.4, 0.2, 0.25)]}
    spec = scenarios.build_spec(menu, data)
    base = next(s for s in scenarios.compute(spec) if s["name"] == "base")
    assert base["price_low"] == pytest.approx((10 * 8 + 100 * 0.44 * 0.7 + (-3) * 5 + 20) * 0.85 / 10, abs=1e-3)  # 总部费用按负价值扣除
    assert base["price_low"] == base["price_high"]  # 倍数区间是一个点
    data["scenarios"][0]["parts"][2]["driver"] = 0                                    # 驱动为 0 没有意义
    with pytest.raises(ValueError, match="不合理"):
        scenarios.build_spec(menu, data)


def test_sotp_scenarios_keep_their_own_holdco_discount():
    seg = lambda d: [V.Segment(name="A", driver=100, multiple=1, discount=d)]  # noqa: E731
    pts = [V.ScenarioPoint(name="bull", driver=0, multiple=1, probability=0.5, segments=seg(0.0), holdco_discount=0.0),
           V.ScenarioPoint(name="base", driver=0, multiple=1, probability=0.5, segments=seg(0.0))]  # 没写 → 用模型默认值
    m = V.ValuationModel(method="sotp", driver_label="d", multiple_label="m", shares=10, price_currency="HKD", listing="HK:01234",
                         holdco_discount=0.2, scenarios=pts)
    assert m.price(m.scenarios[0]) == pytest.approx(10.0) and m.price(m.scenarios[1]) == pytest.approx(8.0)


def test_revise_prompt_includes_the_report_for_derived_numbers(app, monkeypatch):
    """概率加权价这类数字来自报告自己的情景表、不在研究笔记里：修订时要让模型看到报告全文，判为推算而不是删掉。"""
    seen = []
    from stockagent.llm import LLMResult

    def complete(task, system, user, *a, **k):
        seen.append(user)
        return LLMResult(text="", data={"checks": [{"id": 0, "action": "keep", "replacement": "", "evidence": "0.25×21.11+0.5×12.7+0.25×7.09"}]},
                         model="m", profile="p", usage={})
    monkeypatch.setattr(app.llm, "complete", complete)
    report = "## § 12 个月情景\n| 牛 | 25% | 21.11 |\n概率加权价 13.40 港元。\n"
    out, log_, n = dossier.revise_unverified(app, report, "笔记", ["x"])
    assert out == report and log_[0]["action"] == "keep" and n >= 1
    assert "报告全文" in seen[0] and "| 牛 | 25% | 21.11 |" in seen[0]


# ---------------------------------------------------------------- 主锚选择与矩阵 / SOTP 交叉检查

def _both_log(sotp_net_cash=40):
    """同时有矩阵和 SOTP 的工具日志（SOTP 的净现金决定两者基准价的差距）。"""
    log = [_matrix_log()] + [_sotp_call(m, sub, d, holdco_discount=h, net_cash=sotp_net_cash)
                              for m, sub, d, h in (((8, 6), (50, 1.0), 0.3, 0.15), ((10, 8), (60, 1.2), 0.2, 0.1), ((12, 10), (70, 1.4), 0.1, 0.05))]
    return log


def _router(monkeypatch, app, primary, calls, fail_cross=False):
    """假模型：按菜单里出现的估值类型回答；第一次调用（无交叉检查提示）在两类都有时选 primary。"""
    def complete(task, system, user, *a, schema=None, validate=None, **k):
        cross = scenarios.CROSS_SYSTEM in system
        calls.append({"cross": cross, "system": system, "menu": user.split("备选估值：\n")[1].split("\n\n研究笔记")[0]})
        if fail_cross and cross:
            raise LLMError("模型无法给出合格的点")
        menu = calls[-1]["menu"]
        kinds = {"matrix" if "敏感性矩阵" in l else "sotp" for l in menu.splitlines()}
        kind = primary if len(kinds) == 2 else kinds.pop()
        line = next(l for l in menu.splitlines() if ("敏感性矩阵" in l) == (kind == "matrix"))
        idx = int(re.match(r"\[(\d+)\]", line).group(1))
        return LLMResult(text="", data=validate({**(_pts() if kind == "matrix" else _sotp_pts()), "matrix": idx}), model="m", profile="p", usage={})
    monkeypatch.setattr(app.llm, "complete", complete)


def _propose(app, log):
    return scenarios.propose(app, app.watchlist.get("acme"), "笔记", log, "行情", "HKD")


def test_selector_prompt_has_no_sotp_bias_and_follows_the_notes_anchor():
    assert "通常更合适" not in scenarios.SYSTEM and "主锚" in scenarios.SYSTEM and "不要因为某种方法算出的价格更高或更低而改选" in scenarios.SYSTEM
    assert "笔记没有明确指定时选敏感性矩阵" in scenarios.SYSTEM


def test_research_prompts_ask_for_anchor_and_attributable_basis(app):
    val = next(q for k, _, q in dossier.SECTIONS if k == "valuation")
    assert "指定主锚" in val and "归母口径" in val and "少数股东损益" in val
    box = ToolBox(app, app.watchlist.get("acme"))
    desc = next(t["description"] for t in box.specs() if t["name"] == "sotp")
    assert "归母口径" in desc and "少数股东损益" in desc and "stake 填公司实际持有的比例" in desc


def test_cross_check_diverged_sotp_primary_falls_back_to_matrix(app, monkeypatch):
    calls = []
    _router(monkeypatch, app, "sotp", calls)
    spec, note = _propose(app, _both_log(sotp_net_cash=200))  # SOTP 加上大额净现金：基准价远高于矩阵
    assert [c["cross"] for c in calls] == [False, True]
    assert "SOTP" in calls[0]["menu"] and "敏感性矩阵" in calls[0]["menu"]                       # 主锚那次两类都在清单里
    assert "敏感性矩阵" in calls[1]["menu"] and "SOTP" not in calls[1]["menu"]                    # 交叉检查只给另一类
    cc = spec["cross_check"]
    assert spec["kind"] == "matrix" and cc["status"] == "diverged" and cc["switched"] and cc["primary"] == "sotp"
    assert cc["gap"] > scenarios.DIVERGENCE and cc["sotp_base"] > cc["matrix_base"]
    assert "不自动采用 SOTP，退回矩阵" in note and "分歧已标在跟踪卡里" in note
    text = scenarios.table_text(spec, 15.0, "HKD")
    assert "交叉检查：矩阵基准价" in text and "已退回矩阵" in text and "需要在报告中交代" in text


def test_cross_check_diverged_matrix_primary_stays_matrix(app, monkeypatch):
    calls = []
    _router(monkeypatch, app, "matrix", calls)
    spec, note = _propose(app, _both_log(sotp_net_cash=200))
    cc = spec["cross_check"]
    assert spec["kind"] == "matrix" and cc["status"] == "diverged" and not cc["switched"] and "仍以矩阵为主锚" in note
    assert [c["cross"] for c in calls] == [False, True] and "SOTP" in calls[1]["menu"] and "敏感性矩阵" not in calls[1]["menu"]


def test_cross_check_within_threshold_keeps_sotp(app, monkeypatch):
    calls = []
    _router(monkeypatch, app, "sotp", calls)
    spec, note = _propose(app, _both_log(sotp_net_cash=40))
    cc = spec["cross_check"]
    assert spec["kind"] == "sotp" and cc["status"] == "ok" and not cc["switched"] and abs(cc["gap"]) < scenarios.DIVERGENCE and note == ""
    assert "差异在 30% 以内" in scenarios.table_text(spec, 15.0, "HKD")


def test_cross_check_failure_keeps_primary_but_flags_it(app, monkeypatch):
    calls = []
    _router(monkeypatch, app, "sotp", calls, fail_cross=True)
    spec, note = _propose(app, _both_log())
    assert spec["kind"] == "sotp" and spec["cross_check"]["status"] == "unverified" and "未能交叉检查" in note
    assert "交叉检查未完成" in scenarios.table_text(spec, 15.0, "HKD")


def test_single_kind_needs_no_cross_check(app, monkeypatch):
    calls = []
    _router(monkeypatch, app, "matrix", calls)
    spec, note = _propose(app, [_matrix_log()])
    assert len(calls) == 1 and "cross_check" not in spec and note == ""
    calls.clear()
    spec, _ = _propose(app, _sotp_log())
    assert len(calls) == 1 and spec["kind"] == "sotp" and "cross_check" not in spec


def test_card_shows_the_cross_check_divergence(app, monkeypatch):
    _router(monkeypatch, app, "sotp", [])
    spec, _ = _propose(app, _both_log(sotp_net_cash=200))
    card = {"thesis": "论点", "stance": "neutral", "scenarios": scenarios.compute(spec), "valuation_spec": spec, "kill_criteria": [],
            "catalysts": [], "watch_metrics": [], "related_tickers": [], "data_gaps": [], "news_keywords": []}
    md = dossier._card_markdown(app.watchlist.get("acme"), card)
    assert "⚠ 交叉检查：矩阵基准价" in md and "不自动采用 SOTP" in md
    ok = {**card, "valuation_spec": {**spec, "cross_check": {**spec["cross_check"], "status": "ok", "switched": False}}}
    assert "⚠ 交叉检查" not in dossier._card_markdown(app.watchlist.get("acme"), ok) and "交叉检查：" in dossier._card_markdown(app.watchlist.get("acme"), ok)
