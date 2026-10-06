"""fill_gaps 对模型漏字段的容错（2026-10-06 东岳集团：缺 ask 抛 KeyError）。"""
from __future__ import annotations

from stockagent import dossier
from stockagent.llm import LLMResult


def _run(monkeypatch, app, resolved):
    gaps = {"gaps": [{"name": "产能利用率", "why": "判断景气", "queries": ["产能利用率"], "sources": ["年报"]},
                     {"name": "海外收入占比", "why": "判断汇率敞口", "queries": ["海外收入"], "sources": ["年报"]}]}

    def complete(task, system, user, *, schema=None, **k):
        data = gaps if schema is dossier.GAPS_SCHEMA else resolved
        return LLMResult(text="", data=data, model="m", profile="p", usage={})

    monkeypatch.setattr(app.llm, "complete", complete)
    monkeypatch.setattr(app.llm, "run_agent", lambda *a, **k: LLMResult(text="补充笔记", data=None, model="m", profile="p", usage={}))
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
    assert note["text"] == "补充笔记"
    by_item = {r["item"]: r for r in requests}
    assert set(by_item) == {"产能利用率", "海外收入占比", "未命名缺口"}
    assert by_item["产能利用率"]["ask"] == "请补充：产能利用率"
    assert by_item["产能利用率"]["why"] == "判断景气"
    assert by_item["海外收入占比"]["ask"] == "请补充海外收入"


def test_fill_gaps_tolerates_missing_items(monkeypatch, app):
    _note, _log, requests = _run(monkeypatch, app, {})
    assert requests == []
