"""价值陷阱防护：新建仓前的两道闸（只会去掉候选，不会增加或改动仓位；数字仍全部由代码计算）。

1. 风险排除（代码，按市场一次）：funds.yaml 里维护的排除名单（代码、名称关键词）+ 选股器全市场公告扫描（screen_events）
   中的负面事件（清盘 / 违约 / 债务重组、退市风险 / 除牌、非标审计、立案调查）。
2. 模型否决（新建仓前逐只一次）：复用选股器快筛的做法，一次结构化调用（不带工具），只能回答「放行 / 否决 + 一句理由」。
   结果按「标的 + 最新报告期」缓存在 kv（fund_veto:<secid>:<报告期>）；模型不可用时新建仓一律跳过（宁可不买）。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel

from ..app import App
from ..llm import LLMError
from ..utils import iso, log, pct, utcnow
from .config import FundsFile

EVENT_LABEL = {"default": "违约 / 清盘 / 债务重组", "delist_risk": "退市风险 / 除牌", "audit": "非标审计 / 更换核数师",
               "investigation": "立案调查 / 监管处罚"}


def _code_keys(u: dict) -> set[str]:
    m, c = u["market"], u["code"]
    return {c, f"{m}:{c}", c.lstrip("0"), f"{c}.HK" if m == "HK" else c}


def exclusions(app: App, cfg: FundsFile, universe: list[dict]) -> tuple[dict[str, str], dict[str, int]]:
    """secid → 排除原因；以及按来源的命中数。只读库。"""
    ex = cfg.exclusions
    out: dict[str, str] = {}
    hits = {"名单": 0, "名称关键词": 0, "负面公告": 0}
    codes = {k.strip().upper(): v for k, v in ex.codes.items()}
    for u in universe:
        keys = {k.upper() for k in _code_keys(u)}
        if hit := next((codes[k] for k in keys if k in codes), None):
            out[u["secid"]] = f"排除名单：{hit}"
            hits["名单"] += 1
            continue
        name = u.get("name") or ""
        if kw := next((k for k in ex.name_keywords if k in name), None):
            out[u["secid"]] = f"排除名单（名称含「{kw}」）：{ex.name_keywords[kw]}"
            hits["名称关键词"] += 1
    if ex.event_kinds and universe:
        from ..screen import store as sc_store

        since = iso(utcnow() - timedelta(days=ex.event_days))
        sids = {u["secid"] for u in universe}
        for e in sc_store.events_since(app.db, since):
            if e["secid"] in sids and e["secid"] not in out and e["direction"] == "negative" and e["kind"] in ex.event_kinds:
                out[e["secid"]] = f"负面公告（{EVENT_LABEL.get(e['kind'], e['kind'])}）：{e['published_at'][:10]} {e['title'][:40]}"
                hits["负面公告"] += 1
    return out, hits


def event_coverage(app: App, market: str) -> str | None:
    """负面公告扫描覆盖的时间段（提示用）。"""
    from ..screen import store as sc_store

    sc_store.ensure(app.db)
    r = app.db._one("SELECT MIN(published_at) AS a, MAX(published_at) AS b, COUNT(*) AS n FROM screen_events WHERE market=?", (market,))
    if not r or not r["n"]:
        return None
    return f"{r['a'][:10]} 至 {r['b'][:10]}（{r['n']} 条）"


# ---------------------------------------------------------------- 模型否决


class Verdict(BaseModel):
    verdict: Literal["pass", "veto"]
    reason: str


SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["pass", "veto"]}, "reason": {"type": "string"}},
    "required": ["verdict", "reason"],
}

SYSTEM = """你是买方风控分析员，审核一只模拟基金（{fund}）准备新建仓的标的，只回答「放行（pass）」或「否决（veto）」。

有下列任一情况就否决：
- 控股股东、母公司或所属集团处于清盘、破产、债务重组、被接管或退市安排之中，或公司本身有清盘呈请、长期停牌、除牌风险；
- 核数师无法表示意见、辞任且原因不明，或有财务造假、立案调查、大股东占用资金等治理问题；
- 代码算出的高 ROE、高 FCF Yield 明显不可持续：主要来自一次性收益、资产出售、营运资金的短期波动、关联交易，或极低的净资产；
- 主营业务正在萎缩到难以持续（例如依附于已陷入困境的关联方的业务）。

规则：
- 你只能否决，不能推荐、加仓、调整仓位，也不要输出新的数字；输入里的数字都由代码计算。
- 可以使用你掌握的公司背景（例如控股股东的清盘或重组），但要在理由里注明「背景知识，可能过时」。
- 没有具体依据时放行；不要因为行业周期、估值高低或股价走势否决。
- reason 用一句简体中文（60 字以内）说明依据。只输出 JSON。"""


def cache_key(secid: str, period: str | None) -> str:
    return f"fund_veto:{secid}:{period or 'na'}"


def cached(app: App, secid: str, period: str | None) -> dict | None:
    v = app.db.kv_get(cache_key(secid, period))
    try:
        return json.loads(v) if v else None
    except ValueError:
        return None


def context(u: dict, metrics: dict, events: list[dict], flags: list[str]) -> str:
    info = u.get("info") or {}
    fcf = metrics.get("annual_fcf") or []
    lines = [f"## 候选：{u['name']}（{'港股' if u['market'] == 'HK' else 'A 股'} {u['code']}），行业：{u.get('industry') or '—'}",
             f"- 总市值 {u['mcap'] / 1e8:,.0f} 亿 {u.get('currency') or ''}，PE(TTM) {u.get('pe_ttm') or '—'}，PB {u.get('pb') or '—'}",
             (f"- ROE（年化）{info.get('roe') if info.get('roe') is not None else '—'}%，资产负债率 {info.get('debt_ratio') or '—'}%，"
              f"毛利率 {info.get('gross_margin') or '—'}%（上年同期 {info.get('gross_margin_prev') or '—'}%），"
              f"归母净利同比 {info.get('profit_yoy') if info.get('profit_yoy') is not None else '—'}%"),
             f"- 股息率 {pct(info.get('dividend_yield'), sign=False)}，派息比率 {pct(info.get('payout'), sign=False)}",
             (f"- 盈利收益率 {pct(metrics.get('ey'), sign=False)}，保守 FCF Yield {pct(metrics.get('fcf_yield_raw'), sign=False)}，"
              f"净现金 / 市值 {pct(metrics.get('net_cash_ratio'), sign=False)}；近几个年度 FCF（报表币种，亿）："
              + ("、".join(f"{x / 1e8:,.1f}" for x in fcf) or "—")),
             f"- 最新报告期 {info.get('report_date') or '—'}"]
    if flags:
        lines.append("- 代码标记：" + "；".join(flags))
    if events:
        lines.append("### 近期公告标题（代码按标题归类）")
        lines += [f"- {e['published_at'][:10]} {e['title'][:80]}" for e in events[:12]]
    return "\n".join(lines)


def judge(app: App, cfg: FundsFile, fund_name: str, u: dict, metrics: dict, events: list[dict], flags: list[str]) -> dict:
    """一次模型调用。失败时返回 verdict=skip（调用方按「宁可不买」处理）。"""
    try:
        r = app.llm.complete(cfg.veto.task, [SYSTEM.format(fund=fund_name)], context(u, metrics, events, flags) + "\n\n请给出结论（JSON）。",
                             schema=SCHEMA, validate=lambda d: Verdict.model_validate(d).model_dump(), max_tokens=cfg.veto.max_tokens)
    except LLMError as e:
        log.warning("模型否决检查 %s 失败：%s", u["name"], e)
        return {"verdict": "skip", "reason": f"模型调用失败：{str(e)[:60]}"}
    d = r.data or {}
    return {"verdict": d.get("verdict", "skip"), "reason": d.get("reason", ""), "model": r.model, "at": iso(utcnow())}


def check(app: App, cfg: FundsFile, fund_name: str, cands: list[dict], dry_run: bool) -> dict[str, dict]:
    """cands：[{u, metrics, events, flags}] → secid → {verdict: pass|veto|skip, reason, cached}。
    先查缓存；模型不可用时未缓存的一律 skip。dry_run 时不写缓存。"""
    out, todo = {}, []
    for c in cands:
        u = c["u"]
        period = (u.get("info") or {}).get("report_date")
        if hit := cached(app, u["secid"], period):
            out[u["secid"]] = {**hit, "cached": True}
        else:
            todo.append(c)
    if not todo:
        return out
    if reason := app.llm.unavailable_reason(cfg.veto.task):
        for c in todo:
            out[c["u"]["secid"]] = {"verdict": "skip", "reason": f"模型不可用（{reason}）", "cached": False}
        return out
    with ThreadPoolExecutor(max_workers=max(1, app.settings.llm.concurrency)) as ex:
        results = list(ex.map(lambda c: judge(app, cfg, fund_name, c["u"], c["metrics"], c["events"], c["flags"]), todo))
    for c, r in zip(todo, results):
        u = c["u"]
        out[u["secid"]] = {**r, "cached": False}
        if r["verdict"] in ("pass", "veto") and not dry_run:
            app.db.kv_set(cache_key(u["secid"], (u.get("info") or {}).get("report_date")), json.dumps(r, ensure_ascii=False))
    return out
