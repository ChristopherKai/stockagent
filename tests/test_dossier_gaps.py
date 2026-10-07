"""fill_gaps 对模型漏字段的容错（2026-10-06 东岳集团：缺 ask 抛 KeyError）。"""
from __future__ import annotations

from stockagent import dossier
from stockagent.llm import LLMError, LLMResult


def _gaps(n=2):
    names = ["产能利用率", "海外收入占比", "客户集中度", "专利数量", "在手订单", "资本开支", "研发投入", "库存周转"]
    why = ["判断景气", "判断汇率敞口"] + [f"理由{i}" for i in range(2, 8)]
    return {"gaps": [{"name": names[i], "why": why[i], "queries": [names[i]], "sources": ["年报"]} for i in range(n)]}


def _run(monkeypatch, app, resolved, gaps=None, run_agent=None):
    gaps = gaps or _gaps()

    def complete(task, system, user, *, schema=None, **k):
        data = gaps if schema is dossier.GAPS_SCHEMA else resolved
        return LLMResult(text="", data=data, model="m", profile="p", usage={})

    monkeypatch.setattr(app.llm, "complete", complete)
    monkeypatch.setattr(app.llm, "run_agent", run_agent or (lambda *a, **k: LLMResult(text="补充笔记", data=None, model="m", profile="p", usage={})))
    company = app.watchlist.companies[0]
    return dossier.fill_gaps(app, company, {"a": {"title": "t", "text": "x"}}, "行情", lambda *_: None)


def test_fill_gaps_tolerates_missing_fields(monkeypatch, app):
    resolved = {"items": [
        {"name": "产能利用率", "status": "missing", "value": ""},           # 缺 ask
        {"name": "海外收入占比", "ask": "请补充海外收入"},                    # 缺 status → 视为 missing
        {"status": "partial"},                                                # 缺 name 和 ask
        {"name": "已找到项", "status": "found", "value": "1"},               # found 不产出请求
    ]}
    note, _log, requests = _run(monkeypatch, app, resolved)
    assert note["text"] == "补充笔记\n\n补充笔记"  # 2 个缺口 → 2 个 agent 并行，笔记按组拼接
    by_item = {r["item"]: r for r in requests}
    assert set(by_item) == {"产能利用率", "海外收入占比", "未命名缺口"}
    assert by_item["产能利用率"]["ask"] == "请补充：产能利用率"
    assert by_item["产能利用率"]["why"] == "判断景气"
    assert by_item["海外收入占比"]["ask"] == "请补充海外收入"


def test_fill_gaps_tolerates_missing_items(monkeypatch, app):
    _note, _log, requests = _run(monkeypatch, app, {})
    assert requests == []


def test_gap_groups_split_evenly():
    gaps = _gaps(8)["gaps"]
    assert [len(g) for g in dossier._gap_groups(gaps, 3)] == [3, 3, 2]
    assert [len(g) for g in dossier._gap_groups(gaps[:2], 3)] == [1, 1]
    assert [len(g) for g in dossier._gap_groups(gaps, 1)] == [8]
    assert [g[0]["name"] for g in dossier._gap_groups(gaps, 8)] == [x["name"] for x in gaps]  # 顺序保持
    assert [x["name"] for g in dossier._gap_groups(gaps, 3) for x in g] == [x["name"] for x in gaps]


def test_fill_gaps_runs_agents_in_parallel_with_disjoint_gaps(monkeypatch, app):
    import threading
    seen, barrier = [], threading.Barrier(3, timeout=5)  # 3 个 agent 必须同时在跑才能全部通过

    def run_agent(task, system, user, tools, call_tool, **k):
        barrier.wait()
        seen.append(user)
        call_tool("calculate", {"expression": "1+1"})
        return LLMResult(text=f"笔记{len(seen)}", data=None, model="m", profile="p", usage={})

    _note, log_, _req = _run(monkeypatch, app, {}, gaps=_gaps(6), run_agent=run_agent)
    assert len(seen) == 3
    for name in _gaps(6)["gaps"]:  # 每个缺口恰好交给一个 agent
        assert sum(name["name"] in u for u in seen) == 1
    assert len(log_) == 3 and {x["section"] for x in log_} == {"补充研究"}


def test_fill_gaps_single_agent_when_not_parallel(monkeypatch, app):
    app.settings.agent.parallel_sections = False
    calls = []
    note, _l, _r = _run(monkeypatch, app, {}, gaps=_gaps(4),
                        run_agent=lambda *a, **k: calls.append(1) or LLMResult(text="笔记", data=None, model="m", profile="p", usage={}))
    assert len(calls) == 1 and note["text"] == "笔记"


def test_fill_gaps_partial_failure_keeps_other_groups(monkeypatch, app):
    def run_agent(task, system, user, *a, **k):
        if "海外收入占比" in user:
            raise LLMError("上游挂了")
        return LLMResult(text="找到了产能", data=None, model="m", profile="p", usage={})

    note, _l, _r = _run(monkeypatch, app, {}, run_agent=run_agent)
    assert not note["text"].startswith(dossier.FAILED_MARK)  # 部分成功：不整体标记失败，已有结果保留
    assert "找到了产能" in note["text"] and "海外收入占比" in note["text"] and "检索失败" in note["text"]


def test_fill_gaps_all_groups_failed_marks_failed(monkeypatch, app):
    def run_agent(*a, **k):
        raise LLMError("上游挂了")

    note, _l, _r = _run(monkeypatch, app, {}, run_agent=run_agent)
    assert note["text"].startswith(dossier.FAILED_MARK)


def test_write_part_rewrites_empty_output_with_large_budget(monkeypatch, app):
    """金斯瑞前半部分「只有 0 字」：推理型模型的思考用光输出预算。撰写的输出预算要给足（不是 16000），空输出要重写。"""
    full = "# 标题\n\n" + "".join(f"## § {s}\n" + "正文。" * 300 + "\n\n" for s in dossier.PART1_SECTIONS)
    replies, budgets, notes = ["", full], [], []

    def complete(task, system, user, *, max_tokens=16000, **k):
        budgets.append(max_tokens)
        return LLMResult(text=replies.pop(0), data=None, model="m", profile="p", usage={})

    monkeypatch.setattr(app.llm, "complete", complete)
    out = dossier._write_part(app, app.watchlist.companies[0], ["s"], "u", dossier.PART1_SECTIONS, "前半部分", notes.append, title=True)
    assert out == full.strip() and budgets == [dossier.WRITE_MAX_TOKENS] * 2 and dossier.WRITE_MAX_TOKENS > 16000
    assert len(notes) == 1 and "只有 0 字" in notes[0] and "重写第 1 次" in notes[0]
