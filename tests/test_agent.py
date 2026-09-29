"""工具、agent 循环（三种协议）、fallback、深度报告与论点跟踪。"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pandas as pd
import pytest

from stockagent import brief, dossier, monitor
from stockagent.config import LLMConfig
from stockagent.docs import store_document
from stockagent.llm import LLM, LLMError, parse_tool_calls, strip_think
from stockagent.tools import ToolBox, dcf_value, implied_growth, matrix_table, resolve_ticker, safe_calc

CALC = {"name": "calculate", "description": "计算", "parameters": {
    "type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}


# ---------------------------------------------------------------- 计算与估值

def test_safe_calc():
    assert safe_calc("a = 12.3\nb = 45.6\n(b / a) ** (1 / 4) - 1")[0] == pytest.approx((45.6 / 12.3) ** 0.25 - 1)
    assert safe_calc("sum([1, 2, 3]) / len([1, 2, 3])")[0] == 2
    for bad in ("__import__('os')", "(1).real", "'a' * 3", "[1] * 10", "2 ** 1000", "import os", "x"):
        with pytest.raises((ValueError, SyntaxError)):
            safe_calc(bad)


def test_dcf_and_reverse_dcf_roundtrip():
    r = dcf_value(100, 0.12, 0.10, 0.03, years=5, net_cash=50, shares=10)
    assert r["equity_value"] == pytest.approx(r["enterprise_value"] + 50)
    assert implied_growth(r["equity_value"], 100, 0.10, 0.03, 5, net_cash=50) == pytest.approx(0.12, abs=1e-6)
    with pytest.raises(ValueError):
        implied_growth(1000, -5, 0.1, 0.03)
    with pytest.raises(ValueError):
        dcf_value(100, 0.1, 0.03, 0.05)


def test_matrix_table():
    t = matrix_table("净利润", [10, 20], "PE", [5, 10], shares=10, net_cash=0, fx=1.0, current_price=10)
    assert "| 10 | 5.00（-50%） | 10.00（+0%） |" in t
    assert "| 20 | 10.00（+0%） | 20.00（+100%） |" in t


# ---------------------------------------------------------------- 资料检索与代码解析

def test_search_filings_matches_traditional(app, tmp_path):
    company = app.watchlist.get("acme")
    src = tmp_path / "2025年報.txt"
    src.write_text("[第1页]\n公司簡介\n\n[第2页]\n前五名客戶合計佔收益的 23.5%，最大客戶佔 8.1%。\n\n[第3页]\n毛利率為 30%。", encoding="utf-8")
    store_document(app.db, app.data_dir, "acme", src, doc_type="annual_report", period="2025", title="2025年報", source="manual")
    box = ToolBox(app, company)
    out = box.call("search_filings", {"query": "前五名客户 收益"})
    assert "第2页" in out and "23.5%" in out
    doc_id = app.db.documents("acme")[0]["id"]
    assert "毛利率為 30%" in box.call("read_filing", {"doc_id": doc_id, "start_page": 3})
    assert box.call("read_filing", {"doc_id": 999, "start_page": 1}).startswith("错误")
    assert box.call("nope", {}).startswith("错误")
    assert box.call("search_filings", {}).startswith("错误：缺少参数")
    assert len(box.log) == 5


def test_resolve_ticker_formats(app):
    assert resolve_ticker(app, "02577")[0][0] == "116.02577"
    assert resolve_ticker(app, "189.HK")[0][0] == "116.00189"
    assert resolve_ticker(app, "688337.SH")[0][0] == "1.688337"
    assert resolve_ticker(app, "300308")[0][0] == "0.300308"
    assert resolve_ticker(app, "105.NVDA")[0][0] == "105.NVDA"
    assert [x[0] for x in resolve_ticker(app, "测试公司")] == ["0.300001", "116.01234"]


def test_strip_think_and_text_tool_calls():
    assert strip_think("<think>推理</think>\n答案") == "答案"
    assert strip_think("推理过程</think>答案") == "答案"
    calls = parse_tool_calls('先算一下\n<tool_call>\n{"name": "calculate", "arguments": {"code": "1+1"}}\n</tool_call>')
    assert calls == [("calculate", {"code": "1+1"})]
    assert parse_tool_calls('<tool_call>{"name": "x", "arguments": "{\\"a\\": 1}"}</tool_call>') == [("x", {"a": 1})]


# ---------------------------------------------------------------- agent 循环（本地假服务器）

class Queue:
    def __init__(self):
        self.replies: list[dict] = []
        self.requests: list[dict] = []


@pytest.fixture
def fake():
    q = Queue()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            q.requests.append({"path": self.path, "body": body})
            payload = json.dumps(q.replies.pop(0)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    q.url = f"http://127.0.0.1:{httpd.server_port}"
    yield q
    httpd.shutdown()


def _oa(content=None, tool_calls=None, finish="stop"):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"id": "c", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def _llm(profiles: dict) -> LLM:
    return LLM(LLMConfig.model_validate({"profiles": profiles}))


def _calls(record):
    def call(name, args):
        record.append((name, args))
        return f"结果：{safe_calc(args['code'])[0]}"
    return call


def test_openai_native_tool_loop(fake, monkeypatch):
    monkeypatch.setenv("K", "k")
    fake.replies = [
        _oa(tool_calls=[{"id": "t1", "type": "function", "function": {"name": "calculate", "arguments": '{"code": "2*21"}'}}],
            finish="tool_calls"),
        _oa("答案是 42"),
    ]
    record = []
    llm = _llm({"p": {"protocol": "openai", "base_url": fake.url + "/v1", "model": "m", "api_key_env": "K", "max_retries": 0}})
    res = llm.run_agent("research", ["sys"], "问题", [CALC], _calls(record))
    assert res.text == "答案是 42" and record == [("calculate", {"code": "2*21"})]
    second = fake.requests[1]["body"]["messages"]
    assert second[2]["tool_calls"][0]["id"] == "t1"
    assert second[3] == {"role": "tool", "tool_call_id": "t1", "content": "结果：42"}
    assert fake.requests[0]["body"]["tools"][0]["function"]["name"] == "calculate"


def test_openai_text_protocol_and_step_limit(fake, monkeypatch):
    monkeypatch.setenv("K", "k")
    call = '<tool_call>{"name": "calculate", "arguments": {"code": "1+1"}}</tool_call>'
    fake.replies = [_oa(call), _oa(call), _oa("最终答案")]
    record = []
    llm = _llm({"p": {"protocol": "openai", "base_url": fake.url + "/v1", "model": "m", "api_key_env": "K",
                      "tool_mode": "text", "max_retries": 0}})
    res = llm.run_agent("research", ["sys"], "问题", [CALC], _calls(record), max_steps=2)
    assert res.text == "最终答案" and len(record) == 2
    assert "tools" not in fake.requests[0]["body"] and "<tool_call>" in fake.requests[0]["body"]["messages"][0]["content"]
    assert "<tool_response" in fake.requests[1]["body"]["messages"][-1]["content"]
    assert "工具调用次数已用完" in fake.requests[2]["body"]["messages"][-1]["content"]


def test_anthropic_tool_loop(fake, monkeypatch):
    monkeypatch.setenv("K", "k")
    usage = {"input_tokens": 10, "output_tokens": 5}
    fake.replies = [
        {"id": "m1", "type": "message", "role": "assistant", "model": "claude-opus-5", "stop_reason": "tool_use", "stop_sequence": None,
         "content": [{"type": "text", "text": "我来算"}, {"type": "tool_use", "id": "tu1", "name": "calculate", "input": {"code": "6*7"}}],
         "usage": usage},
        {"id": "m2", "type": "message", "role": "assistant", "model": "claude-opus-5", "stop_reason": "end_turn", "stop_sequence": None,
         "content": [{"type": "text", "text": "是 42"}], "usage": usage},
    ]
    record = []
    llm = _llm({"c": {"protocol": "anthropic", "base_url": fake.url, "model": "claude-opus-5", "api_key_env": "K",
                      "refusal_fallback": False, "max_retries": 0}})
    res = llm.run_agent("research", ["sys"], "问题", [CALC], _calls(record))
    assert res.text == "是 42" and record == [("calculate", {"code": "6*7"})]
    first, second = fake.requests[0]["body"], fake.requests[1]["body"]
    assert first["tools"][0]["input_schema"]["required"] == ["code"]
    assert second["messages"][1]["content"][1]["type"] == "tool_use"
    assert second["messages"][2]["content"][0] == {"type": "tool_result", "tool_use_id": "tu1", "content": "结果：42"}


def test_fallback_when_primary_unreachable(fake, monkeypatch):
    monkeypatch.setenv("K", "k")
    with socket.socket() as s:  # 找一个没人监听的端口
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    fake.replies = [_oa('{"ok": true}'), _oa('{"ok": true}')]
    llm = _llm({
        "primary": {"protocol": "openai", "base_url": f"http://127.0.0.1:{dead}/v1", "model": "a", "api_key_env": "K",
                    "max_retries": 0, "fallback": "backup"},
        "backup": {"protocol": "openai", "base_url": fake.url + "/v1", "model": "b", "api_key_env": "K", "max_retries": 0},
    })
    res = llm.complete("triage", ["s"], "u", schema={"type": "object"})
    assert res.profile == "backup" and res.data == {"ok": True}
    assert "primary" in llm._down
    assert "备用 backup:b" in llm.describe("triage")
    with pytest.raises(LLMError):  # 显式指定 profile 时不走 fallback
        llm.complete("triage", ["s"], "u", profile="primary")


def test_fallback_config_must_exist():
    with pytest.raises(ValueError):
        LLMConfig.model_validate({"profiles": {"a": {"model": "x", "fallback": "missing"}}})


# ---------------------------------------------------------------- 深度报告与跟踪

def _seed(app, secid, closes):
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=len(closes)).strftime("%Y-%m-%d")
    app.db.upsert_bars(secid, [{"date": d, "open": c, "close": c, "high": c * 1.01, "low": c * 0.99, "volume": 1e6, "amount": None}
                               for d, c in zip(dates, closes)])


@pytest.mark.parametrize("when,due", [("2026-11-15", "2026-11-15"), ("2026-11", "2026-11-01"), ("2026Q4", "2026-10-01"),
                                      ("2027H2", "2027-07-01"), ("2026年11月", "2026-11-01"), ("明年上半年", None)])
def test_parse_due(when, due):
    assert dossier.parse_due(when) == due


def test_dossier_end_to_end_with_mock(app, monkeypatch):
    monkeypatch.setattr(dossier, "_find_chrome", lambda: None)
    company = app.watchlist.get("acme")
    _seed(app, "0.300001", [50 + i * 0.1 for i in range(60)])
    _seed(app, "116.01234", [40 + i * 0.1 for i in range(60)])
    r = dossier.generate(app, company, progress=lambda *_: None)
    assert r["html_path"].exists() and r["pdf_path"] is None
    html = r["html_path"].read_text(encoding="utf-8")
    assert "深 度 报 告" in html and "跟踪卡" in html and "研究过程与出处" in html
    d = app.db.latest_dossier("acme")
    notes = json.loads(d["notes_json"])
    assert d["card"]["thesis"] and len(notes) == len(dossier.SECTIONS) + 2 and "gapfill" in notes and "snapshot" in notes
    assert json.loads(d["tool_log_json"])  # 模拟 agent 调用过工具
    trig = app.db.triggers("acme")
    assert trig and trig[0]["kind"] == "price" and trig[0]["listing"] == "A:300001"
    assert app.db.plan("acme") is not None
    assert "投资论点" in app.company_context(company)


def test_price_trigger_fires_once_and_alerts(app):
    company = app.watchlist.get("acme")
    _seed(app, "116.01234", [100 - i for i in range(60)])  # 一路下跌，收盘 41
    card = {"thesis": "测试论点", "kill_criteria": []}
    app.db.save_dossier("acme", "mock", "# 报告", card, {}, [], [
        {"description": "H 股跌破 45 港元", "kind": "price", "metric": "close", "listing": "HK:01234", "operator": "<",
         "threshold": 45, "action": "exit"},
        {"description": "距高点回撤超过 70%", "kind": "price", "metric": "drawdown_from_high", "listing": "HK:01234",
         "operator": ">", "threshold": 0.7, "action": "review"},
        {"description": "失去第一大客户", "kind": "event", "action": "reduce"},
    ], [{"when": "2026-12", "due_date": None, "event": "年度业绩", "kpi": "毛利率"}], {"related_tickers": [], "news_keywords": []})
    msgs = monitor.check_price(app, company)
    assert len(msgs) == 1 and "跌破 45" in msgs[0]
    assert monitor.check_price(app, company) == []  # 已触发的不重复提醒
    statuses = {t["description"]: t["status"] for t in app.db.triggers("acme")}
    assert statuses == {"H 股跌破 45 港元": "triggered", "距高点回撤超过 70%": "ok", "失去第一大客户": "ok"}
    out = brief.alerts(app, do_push=False)
    assert "证伪条件触发·退出" in out[0]
    assert app.db.pending_trigger_alerts() == []


def test_paused_primary_is_tried_last():
    import time

    from stockagent.llm import _retryable_status

    llm = _llm({"a": {"protocol": "mock", "model": "a", "fallback": "b"}, "b": {"protocol": "mock", "model": "b"}})
    llm._down["a"] = time.monotonic() + 100
    order = []

    def fn(name, prof):
        order.append(name)
        if name == "b":
            raise LLMError("余额不足", retryable=True)
        return "ok"

    assert llm._with_fallback("a", fn) == "ok" and order == ["b", "a"]
    assert all(_retryable_status(c) for c in (402, 403, 429, 500, 503)) and not any(_retryable_status(c) for c in (400, 401, 404))


def test_dossier_notes_saved_and_reused(app, monkeypatch):
    monkeypatch.setattr(dossier, "_find_chrome", lambda: None)
    company = app.watchlist.get("acme")
    dossier.generate(app, company, progress=lambda *_: None)
    assert dossier.latest_notes(app, company) is not None
    msgs = []
    dossier.generate(app, company, progress=msgs.append, reuse_notes=True)
    assert any("复用研究笔记" in m for m in msgs) and not any("重新补缺" in m for m in msgs)
    # 使用者补充数据后再复用：重新补缺，上一轮的补缺结果保留，补充的数据直接交给撰写
    app.db.upsert_manual("acme", "2027年配额", "2027", 12.5, "万吨", source="生态环境部公告")
    seen = []
    orig = app.llm.complete
    monkeypatch.setattr(app.llm, "complete", lambda task, system, user, *a, **k: seen.append(user) or orig(task, system, user, *a, **k))
    msgs = []
    dossier.generate(app, company, progress=msgs.append, reuse_notes=True)
    assert any("重新补缺" in m for m in msgs)
    notes = json.loads(app.db.latest_dossier("acme")["notes_json"])
    assert "gapfill_prev" in notes and "gapfill" in notes
    assert any("2027年配额" in u and "生态环境部公告" in u for u in seen)


def test_gapfill_rerun_when_search_was_down(app, monkeypatch):
    monkeypatch.setattr(dossier, "_find_chrome", lambda: None)
    company = app.watchlist.get("acme")
    dossier.generate(app, company, progress=lambda *_: None)
    path, data = dossier.latest_notes(app, company)
    data["tool_log"] += [{"tool": "web_search", "args": {"query": "q"}, "section": "补充研究",
                          "result": "没有可用的搜索结果（360搜索：被拦截）"}] * 3
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    msgs = []
    dossier.generate(app, company, progress=msgs.append, reuse_notes=True)
    assert any("网页搜索大多不可用" in m and "重新补缺" in m for m in msgs)


def test_new_dossier_resolves_stale_requests(app):
    ids = app.db.add_requests("acme", None, [{"item": "卖方一致预期", "why": "", "ask": ""}, {"item": "配额", "why": "", "ask": ""}])
    assert len(ids) == 2
    since = "2000-01-01T00:00:00+00:00"
    assert app.db.supplied_since("acme", since) == {"data": 0, "docs": 0, "done": 0}
    app.db.close_request(ids[0], "done")
    assert app.db.supplied_since("acme", since)["done"] == 1
    assert app.db.resolve_requests("acme", ["配额"]) == 0  # 仍然需要的保留
    assert app.db.resolve_requests("acme", []) == 1
    assert app.db.requests("acme") == [] and len(app.db.requests("acme", "resolved")) == 1


def test_new_dossier_is_told_about_previous_triggered_criteria(app, monkeypatch):
    monkeypatch.setattr(dossier, "_find_chrome", lambda: None)
    company = app.watchlist.get("acme")
    app.db.save_dossier("acme", "mock", "# 旧报告", {"thesis": "旧论点"}, {}, [], [
        {"description": "失去第一大客户", "kind": "event", "action": "reduce"}], [], {})
    tid = app.db.triggers("acme")[0]["id"]
    app.db.update_trigger(tid, "triggered", "10-01 公告：第一大客户终止合作")
    assert "失去第一大客户" in dossier.previous_alerts(app, company)
    seen = []
    orig = app.llm.run_agent
    monkeypatch.setattr(app.llm, "run_agent", lambda task, system, user, *a, **k: seen.append(user) or orig(task, system, user, *a, **k))
    dossier.generate(app, company, progress=lambda *_: None)
    assert len(seen) > len(dossier.SECTIONS) and all("第一大客户终止合作" in u for u in seen[: len(dossier.SECTIONS)])
    old = app.db.triggers("acme", active_only=False)
    assert any(t["id"] == tid and t["active"] == 0 and t["status"] == "triggered" for t in old)  # 旧记录保留


def test_forward_return_and_eval_summary(app):
    from datetime import datetime

    from stockagent import evaluate, technical
    from stockagent.utils import CST

    bars = pd.DataFrame({"date": ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"], "close": [10.0, 11.0, 12.0, 9.0]})
    after_close = datetime(2026, 9, 21, 18, 0, tzinfo=CST)
    intraday = datetime(2026, 9, 22, 10, 0, tzinfo=CST)
    assert technical.forward_return(bars, after_close, "A", 2, tradable=False)["ret"] == pytest.approx(0.2)   # 从 21 日收盘起
    assert technical.forward_return(bars, after_close, "A", 2)["ret"] == pytest.approx(9 / 11 - 1)         # 可交易：从 22 日收盘起
    assert technical.forward_return(bars, intraday, "A", 1)["ret"] == pytest.approx(12 / 11 - 1)           # 盘中发布：从当日收盘起
    assert technical.forward_return(bars, after_close, "A", 5) is None
    rows = [{"materiality": 5, "direction": "negative", 1: -0.05, 5: -0.1, 20: None},
            {"materiality": 4, "direction": "negative", 1: 0.02, 5: -0.03, 20: None}]
    table = evaluate.summarize(rows)
    assert "| 4–5 | 利空 | 2 | -1.5% | 50% | 2 | -6.5% | 100% | 0 | — | — |" in table


def test_number_provenance_check():
    from stockagent import verify

    sources = ["净现金 6,065,207,000 元；PE(TTM) 8.6721；毛利率 0.3718", "收盘 11.05 港元，〔2026年中期報告 第16頁〕"]
    report = "股价 11.05 港元，PE 8.7×，毛利率 37.18%，净现金 60.65亿，目标 23.51，见第16页，2026 年。"
    r = verify.check(report, sources)
    assert r.unverified == ["23.51"] and r.total == 5
    assert "23.51" in verify.markdown(r)


def test_gap_claims_and_apply_audit():
    report = ("公司持有东岳硅材的比例未披露。毛利率 37%。\n"
              "| 项目 | 数值 |\n|---|---|\n| 回购 | 未查到 |\n"
              "卖方目标价未查到；配额数量尚未披露。")
    claims = dossier.gap_claims(report)
    assert claims == ["公司持有东岳硅材的比例未披露。", "| 回购 | 未查到 |", "卖方目标价未查到；", "配额数量尚未披露。"]
    fixes = {0: {"replacement": "公司持有东岳硅材约 88.9% 权益〔2025年报 第282页〕。", "evidence": "笔记"},
             1: {"replacement": "回购 2,226.6 万股", "evidence": "列数不对，应被拒绝"}}
    fixed, applied = dossier.apply_audit(report, claims, fixes)
    assert "88.9%" in fixed and "| 回购 | 未查到 |" in fixed and len(applied) == 1


def test_sotp_ddm_and_percentiles():
    from stockagent.tools import ddm_value, percentile_rank, sotp_value

    rows, equity, per_share = sotp_value(
        [{"name": "子公司", "value": 100, "stake": 0.5, "discount": 0.2}, {"name": "主业", "value": 60}],
        shares=10, net_cash=20, fx=2.0, holdco_discount=0.25)
    assert rows[0]["attributable"] == pytest.approx(40) and equity == pytest.approx((40 + 60 + 20) * 0.75)
    assert per_share == pytest.approx(equity / 10 * 2)
    with pytest.raises(ValueError):
        sotp_value([{"name": "x", "value": 1, "stake": 1.5}], shares=1)
    assert ddm_value(1.0, 0.10, 0.02) == pytest.approx(1.02 / 0.08)             # Gordon
    two = ddm_value(1.0, 0.10, 0.02, high_growth=0.10, high_years=2)
    expected = 1.1 / 1.1 + 1.21 / 1.21 + 1.21 * 1.02 / 0.08 / 1.21
    assert two == pytest.approx(expected)
    assert percentile_rank([1, 2, 3, 4], 3) == pytest.approx(0.75)


def test_hk_currency_inference():
    from stockagent.sources.emdata import infer_hk_currency

    cny = {"TOTAL_MARKET_CAP": 19_146_463_589, "ISSUED_COMMON_SHARES": 1_732_711_637, "PE_TTM": 8.67, "EPS_TTM": 1.09}
    hkd = {"TOTAL_MARKET_CAP": 1_000, "ISSUED_COMMON_SHARES": 100, "PE_TTM": 10.0, "EPS_TTM": 1.0}
    usd = {"TOTAL_MARKET_CAP": 7_800, "ISSUED_COMMON_SHARES": 100, "PE_TTM": 10.0, "EPS_TTM": 1.0}
    assert infer_hk_currency([cny]) == "CNY"
    assert infer_hk_currency([hkd]) == "HKD"
    assert infer_hk_currency([usd]) == "USD"
    assert infer_hk_currency([{"PE_TTM": None}]) == "CNY"


def test_financials_tool_formats_money_in_yi(app, monkeypatch):
    from stockagent import tools
    from stockagent.sources import emdata

    rows = [{"期间": "2026中报", "营业收入": 486842894.9, "毛利率%": 54.6454, "币种": "CNY", "_report_date": "2026-06-30"}]
    monkeypatch.setattr(emdata, "financials", lambda session, listing, periods: rows)
    out = tools.ToolBox(app, app.watchlist.get("acme")).call("financials", {"ticker": "300001"})
    assert "| 2026中报 | 4.87 | 54.65 |" in out and "亿 CNY" in out
    assert "只支持 A 股" in tools.ToolBox(app, app.watchlist.get("acme")).call("financials", {"ticker": "NVDA"})


def test_insert_into_section():
    report = "# 标题\n\n## § 现状\n\n现状内容。\n\n## § 资本、现金与稀释\n\n现金充裕。\n\n## § 关键经营变量\n\n配额。\n"
    out = dossier.insert_into_section(report, "资本、现金与稀释", "应收票据从 18.82 亿增至 28.94 亿〔2026中報 第48页〕。")
    assert out.index("应收票据") < out.index("## § 关键经营变量") and out.index("现金充裕") < out.index("应收票据")
    assert dossier.insert_into_section(report, "不存在的章节", "附加").rstrip().endswith("附加")


def test_fcf_snapshot_computes_ev_and_yield(app, monkeypatch):
    from stockagent import tools

    data = {"flows": [
        {"date": "2026-06-30", "annual": False, "ocf": 30e8, "capex": 10e8},
        {"date": "2025-12-31", "annual": True, "ocf": 50e8, "capex": 20e8},
        {"date": "2025-06-30", "annual": False, "ocf": 20e8, "capex": 10e8},
        {"date": "2024-12-31", "annual": True, "ocf": 40e8, "capex": 20e8},
        {"date": "2023-12-31", "annual": True, "ocf": 30e8, "capex": 30e8}],
        "balance": {"date": "2026-06-30", "cash": 80e8, "invest": 20e8, "debt": 10e8, "debt_items": {"短期借款": 10e8},
                    "minority": 0, "liabilities": 50e8}}
    monkeypatch.setattr(tools.emdata, "fcf_data", lambda s, l: data)
    app.db.save_quote("0.300001", {"price": 10.0, "market_cap": 300e8})
    out = tools.fcf_snapshot(app, app.watchlist.get("acme"), forward_fcf=63, forward_label="2027E")
    # TTM：经营现金流 30+50−20=60，资本开支 10+20−10=20，FCF 40；EV = 300 + 10 − (80+20) = 210
    assert "EV = 市值 + 有息负债 + 少数股东权益 − 现金及短期投资 = 210.00" in out
    assert "| TTM（截至 2026-06-30） | 60.00 | 20.00 | 40.00 | 19.0% |" in out
    assert "| 近 3 个年度平均 | | | 16.67 | 7.9% |" in out  # (30 + 20 + 0) / 3 ÷ 210
    assert "按 TTM（截至 2026-06-30） 约 6 年" in out and "（2027E FCF 63 亿）：30.0%" in out
    assert tools._payback_years(-5, 1, 0) == "EV 已为负" and tools._payback_years(10, -1, 0) == "FCF 为负，不适用"


def test_hk_consensus_from_futu_page(app, monkeypatch):
    from stockagent import tools
    from stockagent.config import Listing

    page = ("## 00189 大行評級 \n\n更新時間: 2026/09/28\n\n 近三個月共9位分析師對東岳集團作出評級 \n\n"
            "## 00189 目標價 \n\n更新時間: 2026/09/28\n\n東岳集團股票的目標價預測平均價為18.12，最高價為21.67，最低價為15.25")
    monkeypatch.setattr(tools.web, "exa_fetch", lambda url, n=4000: page if "01234-HK" in url else None)
    app.db.save_quote("116.01234", {"price": 11.05, "market_cap": 1e10})
    out = tools.hk_consensus(app, Listing(market="HK", code="1234"))
    assert "近三个月 9 位分析师" in out and "目标价均值 18.12 港元，最高 21.67，最低 15.25" in out and "+64.0%" in out
    assert "不可用" in tools.hk_consensus(app, Listing(market="HK", code="5"))


def test_incomplete_report_part_is_rewritten(app, monkeypatch):
    from stockagent.llm import LLMResult

    good = "# 标题\n\n" + "\n\n".join(f"## § {h}\n" + "内容。" * 120 for h in dossier.PART1_SECTIONS)
    assert dossier.part_problems(good, dossier.PART1_SECTIONS, title=True) == []
    assert dossier.part_problems("好的，前半部分见上。", dossier.PART1_SECTIONS, title=True)  # 推理占满输出、正文只剩一句
    replies = iter(["好的，前半部分见上。", good])
    monkeypatch.setattr(app.llm, "complete", lambda *a, **k: LLMResult(text=next(replies), data=None, model="m", profile="p", usage={}))
    msgs = []
    out = dossier._write_part(app, app.watchlist.get("acme"), ["s"], "u", dossier.PART1_SECTIONS, "前半部分", msgs.append, title=True)
    assert out == good and any("重写第 1 次" in m for m in msgs)
    monkeypatch.setattr(app.llm, "complete", lambda *a, **k: LLMResult(text="太短", data=None, model="m", profile="p", usage={}))
    with pytest.raises(LLMError, match="连续 3 次输出不完整"):
        dossier._write_part(app, app.watchlist.get("acme"), ["s"], "u", dossier.PART1_SECTIONS, "前半部分", msgs.append, title=True)
