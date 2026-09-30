"""L3 模型快筛：每家一次结构化调用（不带工具），只看代码算好的指标、风险标记与公告标题，
判断「便宜的原因 / 是否价值陷阱 / 催化剂 / 深研要核实的问题」。数字只来自输入；分层由代码决定，
模型的结论只能把候选降为「快筛淘汰」，不能升级（实测模型会在没有任何公告依据时「推测」出催化剂）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Literal

from pydantic import BaseModel, Field

from ..app import App
from ..llm import LLMError
from ..utils import log, pct
from .buckets import BUCKET_LABEL, POOL_LABEL
from .events import LABEL

VERDICT_LABEL = {"deep_dive": "值得深研", "watch": "继续观察", "reject": "淘汰"}


class Catalyst(BaseModel):
    event: str
    timing: str = ""
    evidence: str = ""


class Quick(BaseModel):
    verdict: Literal["deep_dive", "watch", "reject"]
    summary: str
    why_cheap: str = ""
    trap_risks: list[str] = Field(default_factory=list)
    catalysts: list[Catalyst] = Field(default_factory=list)
    catalyst_in_window: bool = False
    questions: list[str] = Field(default_factory=list)


SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["deep_dive", "watch", "reject"]},
        "summary": {"type": "string"},
        "why_cheap": {"type": "string"},
        "trap_risks": {"type": "array", "items": {"type": "string"}},
        "catalysts": {"type": "array", "items": {"type": "object", "properties": {
            "event": {"type": "string"}, "timing": {"type": "string"}, "evidence": {"type": "string"}}, "required": ["event"]}},
        "catalyst_in_window": {"type": "boolean"},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "summary", "why_cheap", "trap_risks", "catalysts", "catalyst_in_window", "questions"],
}

SYSTEM = """你是买方研究的初筛分析员。选股器用代码从全市场筛出了一批候选，你要逐家快速判断是否值得花几分钟做深度研究。
目标是找「高胜率 + 高赔率」的标的，其次是「高赔率」。胜率来自：下行有地板（净现金、稳定现金流）、有明确的催化剂、不是价值陷阱。

规则：
- 只根据给你的数据判断。所有数字都来自代码计算或公告标题，不要编造或改写任何数字；数据里没有的写「需核实」。
- why_cheap：市场为什么给这么低的估值？是错杀（一次性因素、流动性、关注度低）还是有道理（行业下行、现金不可动用、治理问题）？
- trap_risks：价值陷阱的具体风险（现金滞留子公司或大股东财务公司、不分红、关联交易、周期顶点的现金流、会计问题等）。
- catalysts：能让价值兑现的事件（分红 / 特别分红、回购、私有化 / 要约、分拆、资产出售、业绩拐点、纳入港股通或指数等），
  写明依据（哪条公告、前瞻日历的哪一项或哪个指标）和大致时间；没有依据的推测要在 evidence 里写「推测」。catalyst_in_window：长线池看 6–12 个月内、事件池看 1–3 个月内是否有可识别的催化剂。
- verdict：deep_dive 值得深研；watch 有看点但证据不足；reject 明显的陷阱或数据说明不了问题（例如周期顶点、现金显然不可用）。
- questions：深度研究时必须核实的 2–5 个问题，要具体（例如「现金中存放在大股东财务公司的比例」）。
- 这是研究优先级判断，不是买卖建议。只输出 JSON。"""


def _m(v, digits: int = 1) -> str:
    return pct(v, digits, sign=False) if v is not None else "—"


def _yi(v) -> str:
    return f"{v / 1e8:,.1f} 亿" if v is not None else "—"


def context(c: dict, app: App) -> str:
    m = c["metrics"]
    fm, rev, info = m.get("floor"), m.get("revision") or {}, m.get("info") or {}
    lines = [f"## 候选：{c['name']}（{'港股' if c['market'] == 'HK' else 'A 股'} {c['code']}），行业：{c.get('industry') or '—'}",
             f"- 所在池：{POOL_LABEL[c['pool']]}；入选策略桶：{'、'.join(BUCKET_LABEL[b] for b in c['buckets'].split(','))}；"
             f"代码评分 {c['score']}，代码预分层 {c['tier']}",
             f"- 现价 {c['price']:.3g} {c['currency']}（{c.get('price_date') or '—'}），总市值 {_yi(m.get('mcap'))} {c['currency']}，"
             f"PE(TTM) {m.get('pe_ttm') or '—'}，PB {m.get('pb') or '—'}"
             + (f"，股息率 {_m(info.get('dividend_yield'))}，派息比率 {_m(info.get('payout'))}" if c["market"] == "HK" else "")]
    if fm:
        lines += [f"### 现金流与资产负债（代码按报表计算；报表币种 {fm.get('report_ccy') or '—'}，资产负债表截至 {fm['balance_date']}）",
                  f"- EV {_yi(fm['ev'])}，净现金 {_yi(fm['net_cash'])}，净现金 / 市值 {_m(fm['net_cash_ratio'])}，"
                  f"Negative EV：{'是' if fm['negative_ev'] else '否'}",
                  f"- FCF Yield：{fm.get('fcf_base_label') or '最新'} {_m(fm['yield_base'])}，近 3 年平均 {_m(fm['yield_avg3'])}，"
                  f"保守口径 {_m(fm['yield_cons'])}；近 {fm['annual_n']} 个年度 FCF 为正 {fm['fcf_positive_years']} 年；"
                  f"FCF 覆盖 EV 约 {fm['payback_years'] if fm['payback_years'] is not None else '—'} 年",
                  f"- 粗算（假设：合理价值 = 保守 FCF ÷ {app.settings.screen.fair_fcf_yield:.0%} + 净现金；地板 = max(净现金打 "
                  f"{1 - app.settings.screen.cash_haircut:.0%}，保守 FCF ÷ {app.settings.screen.bear_fcf_yield:.0%} + 净现金)）："
                  f"合理价 {fm['fair_price']:.3g}，地板价 {fm['floor_price']:.3g}，上行 {_m(fm['upside'])}，下行 {_m(fm['downside'])}，"
                  f"赔率 {fm['odds']:.1f}"]
    else:
        lines.append("- 现金流口径不适用或缺少报表（金融、地产等行业，或数据缺失）")
    lines.append(f"### 经营（最新报告期 {rev.get('report_date') or '—'}）")
    lines.append(f"- 归母净利同比 {rev.get('profit_yoy') if rev.get('profit_yoy') is not None else '—'}%，收入同比 "
                 f"{rev.get('revenue_yoy') if rev.get('revenue_yoy') is not None else '—'}%，毛利率 {rev.get('gross_margin') or '—'}%"
                 + (f"（同比 {rev['gross_margin_delta']:+.1f} 个百分点）" if rev.get("gross_margin_delta") is not None else ""))
    q, div = m.get("quality") or {}, m.get("dividend") or {}
    if div.get("years") is not None:
        lines.append(f"- 分红：连续派息 {div.get('years')} 年；质量红利检查："
                     + ("全部通过" if q.get("checks") and all(q["checks"].values())
                        else "未通过 " + "、".join(k for k, v in (q.get("checks") or {}).items() if not v) if q.get("checks") else "—"))
    if m.get("calendar"):
        lines.append("### 前瞻日历（代码按公告与规律推算）")
        lines += [f"- {x['date']} {x['text']}{'' if x['confirmed'] else '（推算）'}" for x in m["calendar"][:8]]
    if m.get("themes"):
        lines.append("- 所属产业链主题：" + "；".join(f"{t['theme']} / {t['node']}{'（瓶颈）' if t['bottleneck'] else ''}" for t in m["themes"]))
    if c["flags"]:
        lines.append("### 代码标记的风险")
        lines += [f"- {f['text']}" for f in c["flags"]]
    if c.get("events"):
        lines.append("### 近期公告（代码按标题归类）")
        lines += [f"- {e['published_at'][:10]} [{LABEL.get(e['kind'], e['kind'])}] {e['title'][:80]}" for e in c["events"][:12]]
    if info.get("auditor"):
        lines.append(f"- 核数师：{info['auditor']}")
    return "\n".join(lines)


def screen_one(app: App, c: dict) -> dict | None:
    try:
        r = app.llm.complete("screen", [SYSTEM], context(c, app) + "\n\n请给出快筛结论（JSON）。", schema=SCHEMA,
                             validate=lambda d: Quick.model_validate(d).model_dump(), max_tokens=4000)
    except LLMError as e:
        log.warning("快筛 %s 失败：%s", c["name"], e)
        return None
    return {**(r.data or {}), "model": r.model}


def apply(c: dict, q: dict) -> None:
    """模型结论对分层的影响：只能淘汰，不能升级。"""
    c["quick"] = q
    if q["verdict"] == "reject":
        c["tier"] = "X"


def run(app: App, cands: list[dict], progress: Callable[[str], None] = lambda m: None) -> int:
    if not cands:
        return 0
    progress(f"模型快筛 {len(cands)} 家（{app.llm.describe('screen')}）…")
    with ThreadPoolExecutor(max_workers=max(1, app.settings.llm.concurrency)) as ex:
        results = list(ex.map(lambda c: screen_one(app, c), cands))
    n = 0
    for c, q in zip(cands, results):
        if q:
            apply(c, q)
            n += 1
    return n
