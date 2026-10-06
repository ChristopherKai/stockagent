"""深度报告的反方审稿：引文与依据要经代码核对、修订受数字出处与表格列数约束、不动程序定价的情景章节。"""

from __future__ import annotations

import pytest

from stockagent import review
from stockagent.llm import LLMError, LLMResult

NOTES = ("毛利率 2024 年 31.2%，2025 上半年降至 24.5%，主要因为价格战。\n"
         "前五大客户收入占比 62%，最大客户占 35%。\n")
REPORT = """# 测试：增长可持续

> 导语。

## § 核心洞见
公司毛利率保持稳定，护城河明确。
前五大客户占比 62%，客户结构分散。
| 项目 | 数值 |
| 毛利率 | 31.2% |

## § 12 个月情景
基准情景价格区间 10.00–12.00 元，概率 50%。
"""


def _reply(monkeypatch, app, by_task):
    def complete(task, *a, **k):
        return LLMResult(text="", data=by_task[task], model="m", profile="p", usage={})
    monkeypatch.setattr(app.llm, "complete", complete)


def _data(objections, counter="反方认为毛利率在下滑。"):
    return {"thesis": "增长可持续", "counter_case": counter, "objections": objections}


def _obj(kind="ignored_evidence", severity="high", quote="公司毛利率保持稳定，护城河明确。",
         evidence="2025 上半年降至 24.5%，主要因为价格战", problem="毛利率其实在下滑", fix="写出下滑"):
    return {"kind": kind, "severity": severity, "quote": quote, "evidence": evidence, "problem": problem, "fix": fix}


def test_vet_drops_unverifiable_and_demotes_unevidenced():
    data = _data([
        _obj(),                                                    # 引文、依据都在 → 保持 high
        _obj(quote="报告里根本没有这句话。"),                      # 引文不在报告里 → 丢弃
        _obj(evidence="笔记里没有这段话的原文内容"),               # ignored_evidence 没有依据 → 丢弃
        _obj(kind="overreach", quote="前五大客户占比 62%，客户结构分散。", evidence="", severity="high"),  # 无依据 → 降为 medium
        _obj(kind="missing_risk", quote="", evidence="", severity="high", problem="监管风险未覆盖"),       # 无依据 → low
        _obj(kind="bogus"),                                        # 类别非法 → 丢弃
    ])
    out, dropped = review.vet(data, REPORT, NOTES)
    assert dropped == 3
    assert [(o["kind"], o["severity"]) for o in out] == [("ignored_evidence", "high"), ("overreach", "medium"), ("missing_risk", "low")]


def test_vet_tolerates_markdown_and_quote_style():
    quote = "「公司毛利率保持稳定，护城河明确」。"
    assert review.find_unit(REPORT, quote) == "公司毛利率保持稳定，护城河明确。"
    assert review.find_unit(REPORT, "短句") is None


def test_vet_marks_scenario_section():
    out, _ = review.vet(_data([_obj(kind="overreach", quote="基准情景价格区间 10.00–12.00 元，概率 50%。", evidence="")]), REPORT, NOTES)
    assert out[0]["in_scenarios"] and not review._patchable(out[0])


def test_review_applies_guarded_patches(app, monkeypatch):
    objs = [
        _obj(),
        _obj(kind="overreach", quote="前五大客户占比 62%，客户结构分散。", evidence="最大客户占 35%", problem="集中度高却说分散"),
        _obj(kind="unsupported", quote="| 毛利率 | 31.2% |", evidence="", severity="medium", problem="只给了旧数据"),
    ]
    patches = [
        {"id": 0, "replacement": "公司毛利率已从 31.2% 降至 24.5%（价格战），护城河需要重新评估。", "reason": ""},
        {"id": 1, "replacement": "前五大客户占比 62%，最大客户占 88%，客户集中。", "reason": ""},  # 引入无出处数字 → 丢弃
        {"id": 2, "replacement": "| 毛利率 | 31.2% | 多一列 |", "reason": ""},                    # 列数变了 → 丢弃
    ]
    _reply(monkeypatch, app, {"review": _data(objs), "dossier": {"patches": patches}})
    out, res = review.review(app, REPORT, NOTES, [NOTES])
    assert "已从 31.2% 降至 24.5%" in out and "客户结构分散" in out and "| 毛利率 | 31.2% |" in out
    assert "88%" not in out
    assert len(res["applied"]) == 1 and len(res["unresolved"]) == 2
    assert "## § 12 个月情景" in out and "10.00–12.00" in out
    md = review.markdown(res)
    assert "反方最强论证" in md and "已改写 1 处" in md and "未改动的意见 2 条" in md


def test_review_skips_scenario_section_and_patch_failure(app, monkeypatch):
    calls = []

    def complete(task, *a, **k):
        calls.append(task)
        if task == "dossier":
            raise LLMError("down")
        return LLMResult(text="", data=_data([_obj(kind="overreach", quote="基准情景价格区间 10.00–12.00 元，概率 50%。", evidence="")]),
                         model="m", profile="p", usage={})
    monkeypatch.setattr(app.llm, "complete", complete)
    out, res = review.review(app, REPORT, NOTES, [NOTES])
    assert out == REPORT and calls == ["review"]  # 情景章节不进修订
    assert len(res["unresolved"]) == 1


def test_review_survives_llm_failure(app, monkeypatch):
    def boom(*a, **k):
        raise LLMError("down")
    monkeypatch.setattr(app.llm, "complete", boom)
    out, res = review.review(app, REPORT, NOTES, [NOTES])
    assert out == REPORT and not res["objections"] and review.markdown(res) == ""


def test_review_task_is_routable(app):
    from stockagent.cli import LLM_TASKS
    from stockagent.web.settings_views import TASK_LABEL
    assert "review" in LLM_TASKS and "review" in TASK_LABEL
    assert app.llm.describe("review")  # 未单独配置时回落到默认 profile


def test_provenance_ignores_review_notes():
    from stockagent import dossier
    notes = {"a": {"title": "a", "text": "营收 100 亿"}, "review": {"title": "审稿", "text": "编造的 9999 亿"}}
    src = dossier._provenance_sources("头", notes, [])
    assert not any("9999" in s for s in src)


def test_review_call_uses_large_initial_budget(app, monkeypatch):
    seen = {}

    def complete(task, *a, **k):
        seen[task] = k.get("max_tokens")
        return LLMResult(text="", data=_data([]), model="m", profile="p", usage={})
    monkeypatch.setattr(app.llm, "complete", complete)
    review.review(app, REPORT, NOTES, [NOTES])
    assert seen["review"] == review.REVIEW_MAX_TOKENS > 16000
