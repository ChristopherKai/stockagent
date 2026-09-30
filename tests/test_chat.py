"""报告研读对话：多轮历史的请求构造、report_notes 检索、引文核对与数字溯源、回答流程（mock）、搜索暂停、历史预算。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_llm_http import _llm, server  # noqa: F401 - server 是 fixture
from test_tracker import save_dossier, tapp  # noqa: F401 - tapp 是 fixture

from stockagent import chat, tracker
from stockagent.llm import LLMError, LLMResult, StreamSplitter
from stockagent.tools import ToolBox

NOTES = {"s1": {"title": "财务与估值", "text": "配额 18.53 亿吨，免费发放。\n\n净现金 60.37 亿元来自 2025 年报第 88 页。"}}


def _dossier(app, notes=NOTES) -> int:
    did = save_dossier(app)
    app.db._exec("UPDATE dossiers SET notes_json=? WHERE id=?", (__import__("json").dumps(notes, ensure_ascii=False), did))
    return did


# ---------------------------------------------------------------- 多轮历史

HISTORY = [{"role": "user", "content": "第一问"}, {"role": "assistant", "content": "第一答"}]


def test_run_agent_history_openai(server, monkeypatch):  # noqa: F811
    monkeypatch.setenv("DS_KEY", "k")
    server.reply_text = "第二答"
    llm = _llm({"protocol": "openai", "base_url": server.url + "/v1", "model": "m", "api_key_env": "DS_KEY", "max_retries": 0})
    res = llm.run_agent("chat", ["规则"], "第二问", [], lambda n, a: "", history=HISTORY)
    assert res.text == "第二答"
    msgs = server.requests[-1]["body"]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert [m["content"] for m in msgs[1:]] == ["第一问", "第一答", "第二问"]


def test_run_agent_history_anthropic(monkeypatch):
    monkeypatch.setenv("AK", "k")
    llm = _llm({"protocol": "anthropic", "model": "claude-x", "api_key_env": "AK"})
    seen = []

    def create(**kw):
        seen.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="第二答")], stop_reason="end_turn", model="claude-x",
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    monkeypatch.setattr(llm, "_client", lambda name, prof: SimpleNamespace(messages=SimpleNamespace(create=create)))
    res = llm.run_agent("chat", ["规则"], "第二问", [], lambda n, a: "", history=HISTORY)
    assert res.text == "第二答"
    assert seen[0]["messages"][:3] == [*HISTORY, {"role": "user", "content": "第二问"}]  # 之后追加了本轮的回答


# ---------------------------------------------------------------- report_notes

def test_report_notes_searches_report_notes_and_tool_log(tapp):  # noqa: F811
    c = tapp.watchlist.get("dongyue")
    did = _dossier(tapp)
    box = ToolBox(tapp, c, allow=["report_notes"])
    out = box.call("report_notes", {"query": "配额"})
    assert f"〔深度报告 #{did} 研究笔记·财务与估值〕配额 18.53 亿吨" in out
    assert "报告正文·核心洞见" in out  # 报告正文也可检索
    assert "研究过程 #1" in box.call("report_notes", {"query": "PE"})  # 写报告时的工具调用记录
    assert "没有找到" in box.call("report_notes", {"query": "不存在的词"})
    assert box.call("report_notes", {"query": "配额", "dossier_id": 999}).startswith("错误")
    assert "report_notes" not in tracker.TOOLS  # 只开放给对话


# ---------------------------------------------------------------- 代码核对

REPORT = "## § 核心洞见\n配额是**行政性**供给刚性，\n电子级氟化物放量。\n\n## § 12 个月情景\n基准 18–20 亿 × 8–10×。"


def test_quote_check_and_number_trace():
    answer = ("【报告·核心洞见】「配额是行政性供给刚性」，又说「電子級氟化物放量」。"
              "【报告】「配额是……刚性」\n【报告·情景】「配额是市场化供给」\n"
              "【解释】「刚性」指难以调整。【网页·新浪 09-29】「行业开工率 72%」\n基准利润上限 20 亿，另有 37.5 亿未知。")
    assert chat.report_quotes(answer) == ["配额是行政性供给刚性", "電子級氟化物放量", "配额是……刚性", "配额是市场化供给"]
    res = chat.checks(answer, REPORT, [])
    assert [q["found"] for q in res["quotes"]] == [True, True, True, False]
    assert "37.5亿" in res["unverified"] and "72%" in res["unverified"] and "20亿" not in res["unverified"]
    assert chat.quote_matches("配额是 *行政性* 供给", chat._norm(REPORT))
    # 标签只管到行尾（单独占一行时延续到下一行）；引号写法不同（“” / 『』）不影响核对
    text = "【报告·核心洞见】\n「配额是『行政性』供给刚性」\n另一段「我的概括」\n【报告】原文「电子级氟化物放量」，不是「随口一说」"
    assert chat.report_quotes(text) == ["配额是『行政性』供给刚性", "电子级氟化物放量", "随口一说"]
    assert chat.quote_matches("配额是『行政性』供给刚性", chat._norm(REPORT.replace("**行政性**", "“行政性”")))


# ---------------------------------------------------------------- 回答流程

def test_answer_with_mock_llm(tapp):  # noqa: F811
    c = tapp.watchlist.get("dongyue")
    did = _dossier(tapp)
    tracker.add(tapp, c)
    chat_id, mid = chat.start(tapp, c, did, "估值是怎么算的？", quote="配额是行政性供给刚性")
    assert tapp.db.chat(chat_id)["title"] == "估值是怎么算的？"
    assert tapp.db.chat_message(mid)["status"] == "pending"
    with pytest.raises(chat.ChatError, match="回答中"):
        chat.ask(tapp, chat_id, "追问")
    chat.answer(tapp, mid)
    m = tapp.db.chat_message(mid)
    assert m["status"] == "done" and m["content"] and m["progress"] is None
    assert m["tool_log"] and m["tool_log"][0]["tool"] == "calculate"  # mock agent 调用了一次 calculate
    assert set(m["checks"]) == {"quotes", "numbers_total", "unverified"}
    assert tapp.db._one("SELECT COUNT(*) AS n FROM llm_calls WHERE task='chat'")["n"] == 1

    seen = {}

    def fake(task, system, user, tools, call_tool, **kw):
        seen.update(system=system, user=user, tools=[t["name"] for t in tools], **kw)
        return LLMResult(text="【报告·核心洞见】「配额是行政性供给刚性」", data=None, model="fake", profile="mock", usage={})

    tapp.llm.run_agent = fake
    mid2 = chat.ask(tapp, chat_id, "那净现金呢？")
    chat.answer(tapp, mid2)
    assert [h["role"] for h in seen["history"]] == ["user", "assistant"]
    assert "配额是行政性供给刚性" in seen["history"][0]["content"]  # 上一轮的引用段落
    assert seen["user"].startswith("那净现金呢？")
    assert "讲解员" in seen["system"][0] and "配额是行政性供给刚性" in seen["system"][1]
    assert "估值基线" in seen["system"][2] and tracker.TARGET_LABEL in seen["system"][2]
    assert "report_notes" in seen["tools"] and "web_search" in seen["tools"]
    assert tapp.db.chat_message(mid2)["checks"]["quotes"] == [{"text": "配额是行政性供给刚性", "source": "report", "found": True}]

    def boom(*a, **k):
        raise LLMError("网关不可用")

    tapp.llm.run_agent = boom
    mid3 = chat.ask(tapp, chat_id, "再问")
    chat.answer(tapp, mid3)
    m3 = tapp.db.chat_message(mid3)
    assert m3["status"] == "error" and "网关不可用" in m3["content"]
    # 失败的一轮不进入下一轮的历史
    msgs = tapp.db.chat_messages(chat_id)
    assert len(chat.history(tapp, msgs, None)) == 4


def test_start_validates_input(tapp):  # noqa: F811
    c = tapp.watchlist.get("dongyue")
    did = _dossier(tapp)
    with pytest.raises(chat.ChatError, match="不能为空"):
        chat.start(tapp, c, did, "  ")
    with pytest.raises(chat.ChatError, match="太长"):
        chat.start(tapp, c, did, "问" * (chat.MAX_QUESTION + 1))
    with pytest.raises(chat.ChatError, match="没有这份深度报告"):
        chat.start(tapp, c, did + 1, "问题")


def test_web_search_paused_and_configurable(tapp):  # noqa: F811
    allow, why = chat._allowed_tools(tapp)
    assert "web_search" in allow and not why
    with tapp.lock("dossier") as ok:
        assert ok
        allow, why = chat._allowed_tools(tapp)
        assert "web_search" not in allow and "深度报告正在运行" in why
    tapp.settings.chat.web_search = False
    assert "web_search" not in chat._allowed_tools(tapp)[0]


def test_history_budget_and_turns(tapp):  # noqa: F811
    msgs = []
    for i in range(5):
        msgs.append({"role": "user", "content": f"问{i}", "quote": None, "status": "done"})
        msgs.append({"role": "assistant", "content": "答" * 100, "quote": None, "status": "done"})
    assert len(chat.history(tapp, msgs, None)) == 10
    assert [m["content"] for m in chat.history(tapp, msgs, 210)] == ["问3", "答" * 100, "问4", "答" * 100]
    tapp.settings.chat.max_turns = 1
    assert chat.history(tapp, msgs, None)[0]["content"] == "问4"


def test_context_trims_long_report(tapp):  # noqa: F811
    c = tapp.watchlist.get("dongyue")
    did = _dossier(tapp)
    long = "## § 核心洞见\n关键判断。\n\n## § 附录\n" + "冗长内容。" * 5000
    tapp.db._exec("UPDATE dossiers SET report_md=? WHERE id=?", (long, did))
    d = tapp.db.dossier(did)
    assert "冗长内容" in chat.context(tapp, c, d)[1]
    tapp.settings.llm.profiles["mock"].max_input_chars = 8000
    block = chat.context(tapp, c, d)[1]
    assert "关键判断" in block and "冗长内容" not in block and "report_notes" in block


# ---------------------------------------------------------------- 流式输出

def test_stream_splitter_think_and_tool_call_tags():
    events = []
    sp = StreamSplitter(events.append, hide_tool_calls=True)
    for chunk in ["<thi", "nk>先想一想</th", "ink>答案是", " 42<tool_", 'call>{"name": "x"}</tool_call>', "完"]:
        sp.feed(chunk)
    sp.flush()
    merged = chat.segments(events)
    assert merged == [{"type": "thinking", "text": "先想一想"}, {"type": "text", "text": "答案是 42完"}]
    shown = []
    StreamSplitter(shown.append).feed("a<tool_call>b")  # 原生工具模式：正文里的 <tool_call> 不隐藏
    assert "".join(e["text"] for e in shown) == "a<tool_call>b"


def _chunk(content=None, thinking=None, tool=None, finish=None, usage=None):
    delta = SimpleNamespace(content=content, tool_calls=[tool] if tool else None,
                            model_extra={"reasoning_content": thinking} if thinking else {})
    return SimpleNamespace(model="m", usage=usage, choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


def _tc(id=None, name=None, args=""):
    return SimpleNamespace(index=0, id=id, function=SimpleNamespace(name=name, arguments=args))


def test_openai_streaming_agent(monkeypatch):
    monkeypatch.setenv("DS_KEY", "k")
    llm = _llm({"protocol": "openai", "base_url": "http://127.0.0.1:9/v1", "model": "m", "api_key_env": "DS_KEY"})
    rounds = iter([
        [_chunk(thinking="要算一下"), _chunk(tool=_tc("c1", "calculate")), _chunk(tool=_tc(args='{"code": ')),
         _chunk(tool=_tc(args='"2*3"}'), finish="tool_calls")],
        [_chunk(thinking="得到 6"), _chunk(content="结果"), _chunk(content="是 6", finish="stop"),
         SimpleNamespace(model="m", usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5), choices=[])],
    ])
    seen = []

    def create(**kw):
        seen.append(kw)
        return iter(next(rounds))

    monkeypatch.setattr(llm, "_client", lambda name, prof: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    events, calls = [], []

    def call_tool(name, args):
        calls.append((name, args))
        return "结果：6"

    res = llm.run_agent("chat", ["规则"], "算 2*3", [{"name": "calculate", "description": "", "parameters": {}}], call_tool,
                        on_event=events.append)
    assert res.text == "结果是 6" and calls == [("calculate", {"code": "2*3"})]
    assert seen[0]["stream"] is True and seen[0]["stream_options"] == {"include_usage": True}
    assert seen[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "结果：6"}
    assert chat.segments(events) == [{"type": "thinking", "text": "要算一下得到 6"}, {"type": "text", "text": "结果是 6"}]


def test_anthropic_streaming_agent(monkeypatch):
    monkeypatch.setenv("AK", "k")
    llm = _llm({"protocol": "anthropic", "model": "claude-x", "api_key_env": "AK"})
    final = SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="想"), SimpleNamespace(type="text", text="答")],
                            stop_reason="end_turn", model="claude-x", usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="thinking_delta", thinking="想"))
            yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="答"))
            yield SimpleNamespace(type="message_stop")

        def get_final_message(self):
            return final

    monkeypatch.setattr(llm, "_client", lambda name, prof: SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: Stream())))
    events = []
    res = llm.run_agent("chat", ["规则"], "问", [], lambda n, a: "", on_event=events.append)
    assert res.text == "答" and events == [{"type": "thinking", "text": "想"}, {"type": "text", "text": "答"}]


def test_segments_restart_and_tools():
    events = [{"type": "thinking", "text": "想"}, {"type": "tool", "name": "calculate", "label": "计算", "args": {"code": "1"}},
              {"type": "tool_result", "result": "结果：1", "error": False}, {"type": "text", "text": "半截"},
              {"type": "restart", "profile": "qwen"}, {"type": "text", "text": "完整答案"}, {"type": "done"}]
    segs = chat.segments(events)
    assert [s["type"] for s in segs] == ["tool", "note", "text"]
    assert segs[0]["result"] == "结果：1" and "qwen" in segs[1]["text"] and segs[2]["text"] == "完整答案"


def test_answer_saves_trace_and_web_switch(tapp):  # noqa: F811
    c = tapp.watchlist.get("dongyue")
    did = _dossier(tapp)
    _, mid = chat.start(tapp, c, did, "问题")
    chat.answer(tapp, mid, web=False)
    m = tapp.db.chat_message(mid)
    types = [s["type"] for s in m["trace"]]
    assert types == ["note", "thinking", "tool", "text"] and "关闭了联网搜索" in m["trace"][0]["text"]
    assert m["trace"][2]["name"] == "calculate" and m["trace"][2]["result"] == "结果：2"
    tr = chat.get_trace(mid)
    assert tr.done_at is not None and tr.events[-1] == {"type": "done"}
    allow, _ = chat._allowed_tools(tapp, False)
    assert "web_search" not in allow and "fetch_url" not in allow and "report_notes" in allow
    assert "fetch_url" in chat._allowed_tools(tapp, True)[0]


def test_trace_column_migration(tmp_path):
    import sqlite3

    from stockagent.db import DB

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE chats (id INTEGER PRIMARY KEY, company_id TEXT NOT NULL, dossier_id INTEGER NOT NULL, "
                       "title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL); "
                       "CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, role TEXT NOT NULL, "
                       "content TEXT NOT NULL DEFAULT '', quote TEXT, status TEXT NOT NULL DEFAULT 'done', progress TEXT, "
                       "checks_json TEXT NOT NULL DEFAULT '{}', tool_log_json TEXT NOT NULL DEFAULT '[]', model TEXT, profile TEXT, "
                       "usage_json TEXT, created_at TEXT NOT NULL); PRAGMA user_version=1;")
    conn.close()
    db = DB(path)
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(chat_messages)")}
    assert "trace_json" in cols and db.conn.execute("PRAGMA user_version").fetchone()[0] == 3
    cid = db.create_chat("x", 1, "t", revision_id=5)
    assert db.chat(cid)["revision_id"] == 5
    mid = db.add_chat_message(cid, "assistant", "答")
    assert db.chat_message(mid)["trace"] == []
    db.close()
    DB(path).close()  # 再次打开：已是最新版本，不重复加列


# ---------------------------------------------------------------- 修正页研讨

EVENTS = [
    {"code": "aaa", "kind": "event", "impact": "none", "materiality": 5, "note": "旧闻", "url": "https://x.y/a",
     "text": "id=aaa｜09-01 12:22｜港交所｜重要性5｜利好｜订单/客户｜A 公告。｜预期：符合｜发布后：+1.0%·3日｜原文：https://x.y/a"},
    {"code": "bbb", "kind": "event", "impact": "material", "materiality": 3, "note": "上调利润",
     "text": "id=bbb｜09-02 10:00｜新闻｜重要性3｜利好｜财务｜B 长协价上调 5%。｜待跟踪：Q4 执行"},
    {"code": "ccc", "kind": "event", "impact": "material", "materiality": 4, "note": "影响配额",
     "text": "id=ccc｜09-03 10:00｜新闻｜重要性4｜利空｜政策｜C 配额调减。"},
    {"code": "K7", "kind": "trigger", "impact": "minor", "materiality": 0, "note": "苗头",
     "text": "K7｜证伪条件由「正常」升级为「苗头」：R32 跌破 6 万"},
]


def test_evidence_sorted_by_impact_then_materiality():
    from stockagent.web.views import _evidence, _flags

    ev = _evidence(EVENTS)
    assert [e["code"] for e in ev] == ["ccc", "bbb", "K7", "aaa"]
    a = ev[-1]
    assert a["meta"] == ["09-01 12:22", "港交所", "利好", "订单/客户"] and a["title"] == "A 公告。"
    assert a["reaction"] == "+1.0%·3日" and a["extra"] == [("预期", "符合")]
    assert ev[2]["title"].startswith("证伪条件由")
    assert _flags(["待确认的口径问题：净现金口径——相差 197 亿", "情景价格顺序异常"]) == [
        {"tag": "待确认的口径问题", "title": "净现金口径", "detail": "相差 197 亿"},
        {"tag": "", "title": "情景价格顺序异常", "detail": ""}]


def _revision(app) -> dict:
    c = app.watchlist.get("dongyue")
    _dossier(app)
    tracker.add(app, c)
    rid = app.db.latest_revision("dongyue")["id"]
    app.db._exec("UPDATE revisions SET events_json=?, notes_md=?, summary=?, flags_json=? WHERE id=?",
                 (__import__("json").dumps(EVENTS, ensure_ascii=False), "长协价上调带来 2027 年利润上修。", "利润上修，维持立场",
                  '["待确认的口径问题：净现金口径——相差 197 亿"]', rid))
    return app.db.revision(rid)


def test_revision_chat_context_and_checks(tapp):  # noqa: F811
    c = tapp.watchlist.get("dongyue")
    rev = _revision(tapp)
    with pytest.raises(chat.ChatError, match="没有这条修正记录"):
        chat.start(tapp, c, 0, "问题", revision_id=rev["id"] + 99)
    chat_id, mid = chat.start(tapp, c, 0, "这次改了什么？", revision_id=rev["id"])
    assert tapp.db.chat(chat_id)["revision_id"] == rev["id"] and tapp.db.chat(chat_id)["dossier_id"] == rev["dossier_id"]
    seen = {}

    def fake(task, system, user, tools, call_tool, **kw):
        seen["system"] = system
        return LLMResult(text=f"【修正 #{rev['id']}·研究笔记】「长协价上调带来 2027 年利润上修」\n【修正】「长协价大涨」\n"
                              "【报告·核心洞见】「配额是行政性供给刚性」", data=None, model="f", profile="mock", usage={})

    tapp.llm.run_agent = fake
    chat.answer(tapp, mid)
    assert f"修正 #{rev['id']}" in seen["system"][0] and len(seen["system"]) == 4
    block = seen["system"][2]
    assert "长协价上调带来 2027 年利润上修" in block and "ccc［" in block and "净现金口径" in block
    quotes = tapp.db.chat_message(mid)["checks"]["quotes"]
    assert [(q["source"], q["found"]) for q in quotes] == [("revision", True), ("revision", False), ("report", True)]
