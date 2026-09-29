"""公司画像：用年报、中期报告、研报等资料生成，作为分级和简报的背景知识。"""

from __future__ import annotations

import re

from .app import App
from .config import Company
from .docs import DOC_TYPES, read_document_text
from .llm import LLMError

PROFILE_SYSTEM = """你是资深行业研究员。根据提供的公司资料（年报、中期报告、研报、调研纪要、使用者笔记等），为后续的新闻分级和交易研究写一份"公司画像"。

要求：
- 只使用资料中的信息。资料没有的写"资料未披露"，不要凭记忆补充数字。
- 关键数字注明出处，格式如〔2025年报 第35页〕。
- 简体中文，Markdown，总长度控制在 2500 字以内。

结构：
## 一句话定位
## 业务与收入结构（分业务/分地区，最新一期数据及同比）
## 客户与供应商（集中度、主要客户/供应商，如有披露）
## 竞争格局与护城河
## 核心驱动因素（3–6 个可跟踪的量化指标，如价格、销量、产能利用率、毛利率）
## 主要风险（按重要性排序）
## 近期关注事项（资料里提到的时间节点：解禁、投产、新产品、诉讼进展、业绩指引等）
## 产业链位置（上游/下游与关键环节；哪些外部新闻会影响本公司）"""

# 预算不够放全文时，优先保留这些章节附近的内容
KEYWORDS = [
    "管理層討論及分析", "管理层讨论与分析", "業務回顧", "业务回顾", "經營情況討論", "经营情况讨论",
    "主營業務", "主营业务", "核心競爭力", "核心竞争力", "未來展望", "未来展望", "發展戰略", "发展战略",
    "風險因素", "风险因素", "主要風險", "重大风险", "可能面对的风险", "主要客戶", "前五名客户",
    "主要供應商", "前五名供应商", "行業概覽", "行业情况", "Management Discussion", "Business Review",
    "Principal Risks",
]


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def select_text(text: str, budget: int) -> tuple[str, bool]:
    """返回 (节选文本, 是否节选)。
    定期报告前半部分（公司简介、管理层讨论、董事会报告）信息密度最高，先保留开头一半预算，
    再在后文中围绕关键章节（风险、客户、供应商等）取窗口，预算有剩余就顺着开头继续读。"""
    if len(text) <= budget:
        return text, False
    head = int(budget * 0.5)
    hits = [m.start() for kw in KEYWORDS for m in list(re.finditer(re.escape(kw), text[head:]))[:2]]
    hits = [h + head for h in hits]
    window = min(30000, max(4000, (budget - head) // max(1, len(hits))))
    merged = _merge([(0, head)] + [(max(0, s - 300), min(len(text), s + window)) for s in hits])
    used = sum(e - s for s, e in merged)
    if used < budget:
        merged = _merge(merged + [(0, min(len(text), head + budget - used))])
    parts, used = [], 0
    for s, e in merged:
        if used >= budget:
            break
        take = min(e - s, budget - used)
        parts.append(text[s : s + take])
        used += take
    return "\n\n……（中略）……\n\n".join(parts), True


def pick_documents(docs: list[dict]) -> list[dict]:
    """最新年报、最新中期报告，加最多 3 份其他资料（研报/纪要/笔记等）。没有年报时用招股书。"""
    def latest(doc_type):
        cands = [d for d in docs if d["doc_type"] == doc_type and d["chars"]]
        return max(cands, key=lambda d: (d["period"] or "", d["published_at"] or d["added_at"]), default=None)

    picked = [d for d in (latest("annual_report"), latest("interim_report")) if d]
    if not any(d["doc_type"] == "annual_report" for d in picked) and (p := latest("prospectus")):
        picked.append(p)
    others = [d for d in docs if d["doc_type"] in ("research", "transcript", "note", "other", "quarterly_report") and d["chars"]]
    picked += others[:3]  # documents() 已按时间倒序
    return picked


def _budgets(docs: list[dict], total: int) -> list[int]:
    weight = {"annual_report": 5, "prospectus": 5, "interim_report": 3}
    w = [weight.get(d["doc_type"], 1) for d in docs]
    raw = [total * x / sum(w) for x in w]
    # 短文档用不完的预算让给其他文档
    need = [min(d["chars"], int(r)) for d, r in zip(docs, raw)]
    spare = total - sum(need)
    for i, d in enumerate(docs):
        if spare <= 0:
            break
        extra = min(spare, d["chars"] - need[i])
        need[i] += extra
        spare -= extra
    return need


def needs_profile(app: App, company: Company) -> str | None:
    docs = pick_documents(app.db.documents(company.id))
    if not docs:
        return None
    prof = app.db.profile(company.id)
    if prof is None:
        return "尚未生成"
    if set(prof["doc_ids"]) != {d["id"] for d in docs}:
        return "有新资料"
    return None


def generate(app: App, company: Company, force: bool = False) -> str:
    docs = pick_documents(app.db.documents(company.id))
    if not docs:
        return f"{company.name}：没有可用资料（运行 stock docs fetch，或把文件放进 data/inbox/{company.id}/）"
    if not force and needs_profile(app, company) is None:
        return f"{company.name}：画像已是最新"
    reason = app.llm.unavailable_reason("profile")
    if reason:
        return f"{company.name}：跳过画像生成（{reason}）"

    _, prof, _ = app.settings.llm.route("profile")
    total = app.settings.report.profile_max_doc_chars
    if prof.max_input_chars:
        total = min(total, prof.max_input_chars)
    budgets = _budgets(docs, total)
    sections, notes = [], []
    for i, (d, b) in enumerate(zip(docs, budgets), 1):
        text, cut = select_text(read_document_text(d), b)
        if cut:
            notes.append(f"《{d['title']}》原文 {d['chars']:,} 字，按关键章节节选 {len(text):,} 字")
        pages = f"｜{d['pages']} 页" if d["pages"] else ""
        sections.append(
            f"=== 文档{i}：{d['title']}｜{DOC_TYPES.get(d['doc_type'], d['doc_type'])} {d['period'] or ''}"
            f"｜来源 {d['source']}{pages} ===\n{text}"
        )
    user = (
        f"{app.company_context(company).split('### 公司画像')[0].strip()}\n\n"
        f"以下是{company.name}的 {len(docs)} 份资料"
        + (f"（{'；'.join(notes)}）" if notes else "")
        + "。请写公司画像。\n\n" + "\n\n".join(sections)
    )
    try:
        res = app.llm.complete("profile", [PROFILE_SYSTEM], user, max_tokens=16000)
    except LLMError as e:
        return f"✗ {company.name} 画像生成失败：{e}"
    app.db.save_profile(company.id, res.text.strip(), [d["id"] for d in docs], res.model)
    tail = f"（{'；'.join(notes)}；上限由 report.profile_max_doc_chars 与模型的 max_input_chars 决定）" if notes else ""
    return f"{company.name}：画像已生成，基于 {len(docs)} 份资料{tail}"
