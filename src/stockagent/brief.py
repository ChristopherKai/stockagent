"""每日简报：市场概览、个股行情与技术面、已分级事件及价格反应、LLM 研判、数据缺口。"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import markdown as md

from . import monitor, notify, technical, tracker
from .app import MARKET_LABEL, SOURCE_LABEL, App
from .config import Company, Listing
from .docs import DOC_TYPES
from .dossier import ACTION_LABEL
from .llm import LLMError
from .profile import needs_profile
from .triage import DIRECTIONS, event_label, listing_market
from .utils import CST, big_num, fmt_local, iso, num, parse_iso, pct, today_local, utcnow

DISCLAIMER = "本报告由程序自动生成，仅供研究参考，不构成投资建议。"

STOCK_BRIEF_SYSTEM = """你是买方研究员，为一位以港股为主、做波段和事件驱动交易的投资者写每日个股简报。

原则：
- 只基于给出的数据和已分级事件，不要编造数字。引用事件时标注日期与来源，如〔09-23 港交所〕。
- 先说结论：今天这只股票最重要的变化是什么、有多重要；没有重要变化就直说"无重要新增信息"。
- 结合价格反应（发布后涨跌、超额收益、量比、趋势）判断消息是否已被市场消化。
- 不给买卖建议，也不要自行给出目标价（目标价由「标的追踪」按估值假设计算）；只给出需要验证的问题，以及接下来要盯的催化剂和风险。
- 简体中文，Markdown，不超过 400 字，不要写标题行。

输出结构（每项用加粗小标题开头）：
**结论**：一两句话，并给出关注级别（高/中/低）。
**关键变化**：最多 4 条要点。
**价格与预期**：价格是否已反映消息，技术状态如何。
**待验证/待跟踪**：2–4 条。"""

HEADLINE_SYSTEM = """你是投资组合的晨会主持人。根据各股简报和市场数据写"今日要点"：
- 3–6 条，按重要性排序，每条一句话，点名公司并说明为什么重要。
- 多只股票受同一因素影响（如 AI 算力链、半导体情绪、港股流动性、汇率）时合并说明。
- 不给买卖建议。简体中文，Markdown 无序列表，不要标题。"""

HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ --bg:#fbfbf9; --fg:#1f1f1c; --muted:#6b6a64; --line:#e3e1da; --card:#ffffff; --up:#c0392b; --down:#1e8449; --accent:#2f5d8a; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#16171a; --fg:#e6e4dd; --muted:#9a988f; --line:#2e3036; --card:#1d1f23; --up:#e2665a; --down:#4fbf7f; --accent:#7fb0e0; }}
}}
body {{ background:var(--bg); color:var(--fg); font:15px/1.65 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; margin:0; }}
main {{ max-width:1100px; margin:0 auto; padding:24px 16px 64px; }}
h1 {{ font-size:24px; margin:8px 0 4px; }} h2 {{ font-size:19px; margin-top:36px; border-bottom:1px solid var(--line); padding-bottom:6px; }}
h3 {{ font-size:17px; margin-top:28px; }}
blockquote {{ color:var(--muted); margin:0; padding:0 0 0 12px; border-left:3px solid var(--line); }}
table {{ border-collapse:collapse; width:100%; margin:10px 0; font-size:13.5px; display:block; overflow-x:auto; }}
th, td {{ border-bottom:1px solid var(--line); padding:6px 8px; text-align:left; vertical-align:top; }}
th {{ color:var(--muted); font-weight:500; white-space:nowrap; }}
a {{ color:var(--accent); text-decoration:none; }}
code {{ background:var(--card); border:1px solid var(--line); border-radius:4px; padding:0 4px; font-size:13px; }}
.up {{ color:var(--up); }} .down {{ color:var(--down); }}
</style></head><body><main>
{body}
</main></body></html>"""


@dataclass
class CompanyBrief:
    company: Company
    snaps: list[tuple[Listing, dict | None]]
    ah: dict | None
    events: list[dict]
    earlier: list[dict]
    untriaged: int
    pending_filings: list[dict]
    manual: list[dict]
    gaps: list[str]
    thesis: str | None = None
    thesis_date: str | None = None
    triggers: list[dict] = field(default_factory=list)
    catalysts: list[dict] = field(default_factory=list)
    related: list[tuple[dict, dict | None]] = field(default_factory=list)
    tracking: dict | None = None     # 标的追踪摘要（目标价由代码计算，只写进简报，不交给研判 LLM）
    analysis: str | None = None
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 数据缺口

def expected_periods(today) -> tuple[str, str]:
    """应已披露的最新年报与中期报告期间（港股/A股年报 4 月底前、中报 8–9 月披露）。"""
    annual = str(today.year - 1) if today.month >= 5 else str(today.year - 2)
    interim = f"{today.year}H1" if today.month >= 9 else f"{today.year - 1}H1"
    return annual, interim


def company_gaps(app: App, company: Company, snaps) -> list[str]:
    gaps = []
    docs = app.db.documents(company.id)
    have = {(d["doc_type"], d["period"]) for d in docs}
    annual, interim = expected_periods(today_local())
    inbox = f"data/inbox/{company.id}/"
    if ("annual_report", annual) not in have:
        gaps.append(f"缺 {annual} 年报：运行 `stock docs fetch {company.id}` 自动下载，或把 PDF 放进 `{inbox}`")
    if ("interim_report", interim) not in have:
        gaps.append(f"缺 {interim} 中期报告：运行 `stock docs fetch {company.id}`，或把 PDF 放进 `{inbox}`")
    for d in docs:
        if (d["chars"] or 0) < 1000 and (d["file_path"] or "").lower().endswith(".pdf"):
            gaps.append(f"《{d['title']}》没有抽取到文字（可能是扫描件），请换成可复制文字的版本")
    if docs and (reason := needs_profile(app, company)):
        gaps.append(f"公司画像{reason}：运行 `stock profile {company.id}`")
    for listing, snap in snaps:
        if snap is None:
            gaps.append(f"{listing.display} 没有行情数据：运行 `stock fetch {company.id}`")
        elif (today_local() - datetime.strptime(snap["date"], "%Y-%m-%d").date()).days > 7:
            gaps.append(f"{listing.display} 行情停在 {snap['date']}（停牌或抓取失败）")
    for r in app.db.requests(company.id)[:4]:
        gaps.append(f"需要你补充（#{r['id']}）：{r['item']}——{r['ask']}")
    if company.listing("US") and not app.settings.sources.sec_user_agent:
        gaps.append("美股 SEC 文件未抓取：在 config.yaml 设置 sources.sec_user_agent（姓名 + 邮箱）")
    return gaps


# ---------------------------------------------------------------- 汇总数据

def gather(app: App, company: Company, since: str) -> CompanyBrief:
    rc = app.settings.report
    snaps = [(l, app.listing_snapshot(l)) for l in company.listings]
    # items_since 已按时间倒序；稳定排序后即为"重要性降序、同级按时间倒序"
    events = sorted(app.db.items_since(company.id, since, min_materiality=rc.min_materiality),
                    key=lambda e: -(e["materiality"] or 0))[: rc.max_items_per_company]
    week_ago = iso(utcnow() - timedelta(days=7))
    earlier = [e for e in app.db.items_since(company.id, week_ago, min_materiality=4) if e["published_at"] < since][:6]
    for e in events + earlier:
        market = listing_market(app, company, e)
        listing = company.listing(market) or company.listings[0]
        e["reaction"] = technical.reaction(
            app.bars(app.secid(listing)), parse_iso(e["published_at"]), listing.market,
            app.bars(app.bench_secid(listing)),
        )
    untriaged = [e for e in app.db.items_since(company.id, since) if e["triaged_at"] is None]
    cb = CompanyBrief(
        company=company, snaps=snaps, ah=app.ah_premium(company), events=events, earlier=earlier,
        untriaged=len(untriaged), pending_filings=[e for e in untriaged if e["kind"] == "announcement"][:10],
        manual=app.db.manual_data(company.id)[:12], gaps=company_gaps(app, company, snaps),
    )
    if d := app.db.latest_dossier(company.id):
        cb.thesis, cb.thesis_date = d["card"]["thesis"], d["created_at"][:10]
        cb.triggers = sorted(app.db.triggers(company.id), key=lambda t: -monitor.RANK.get(t["status"], 0))
        cb.catalysts = monitor.upcoming(app, company)
        plan = app.db.plan(company.id) or {}
        cb.related = [(r, technical.snapshot(app.bars(r["secid"]))) for r in plan.get("related_tickers", []) if r.get("secid")]
        cb.tracking = tracker.status(app, company)
        stale = app.settings.report.dossier_stale_days
        if (today_local() - datetime.strptime(cb.thesis_date, "%Y-%m-%d").date()).days >= stale:
            cb.gaps.append(f"深度报告已超过 {stale} 天：运行 `stock dossier {company.id}` 更新论点与证伪条件")
    else:
        cb.gaps.append(f"还没有深度报告（论点、证伪条件、跟踪计划）：运行 `stock dossier {company.id}`")
    return cb


def _reaction_text(r: dict | None) -> str:
    if not r:
        return "—"
    if r["days"] == 0 or r["ret"] is None:
        return "尚未交易"
    s = f"{pct(r['ret'])}"
    if r.get("excess") is not None:
        s += f"（超额 {pct(r['excess'])}）"
    return s + f"·{r['days']}日"


def _event_line(e: dict) -> str:
    src = SOURCE_LABEL.get(e["source"], e["source"])
    parts = [
        f"〔{fmt_local(e['published_at'])} {src}〕重要性{e['materiality']} {DIRECTIONS.get(e['direction'], '')} · {event_label(e)}：{e['summary_zh'] or e['title']}",
    ]
    if e.get("expectation"):
        parts.append(f"预期：{e['expectation']}")
    parts.append(f"发布后：{_reaction_text(e.get('reaction'))}")
    if e.get("follow_up"):
        parts.append(f"待跟踪：{e['follow_up']}")
    return "｜".join(parts)


def analyze(app: App, cb: CompanyBrief) -> None:
    c = cb.company
    lines = [f"今天是 {today_local():%Y-%m-%d}（北京时间）。", "", "## 行情与技术面", app.market_line(c), ""]
    lines.append(f"## 最近 {app.settings.report.lookback_hours} 小时的已分级事件（按重要性排序）")
    lines += [f"{i}. {_event_line(e)}" for i, e in enumerate(cb.events, 1)] or ["（无）"]
    if cb.untriaged:
        lines.append(f"（另有 {cb.untriaged} 条尚未分级）")
    if cb.earlier:
        lines += ["", "## 过去 7 天内更早的重要事件（重要性≥4）"] + [f"- {_event_line(e)}" for e in cb.earlier]
    if cb.manual:
        lines += ["", "## 使用者手动补充的数据"] + [
            f"- {m['metric']}（{m['period']}）：{m['value'] if m['value'] is not None else ''}{m['unit'] or ''}"
            + (f"，{m['note']}" if m["note"] else "") for m in cb.manual
        ]
    if cb.triggers:
        lines += ["", "## 证伪条件状态（来自深度报告）"] + [
            f"- [{STATUS_LABEL[t['status']]}] {t['description']}" + (f"｜依据：{t['evidence']}" if t["evidence"] and t["status"] != "ok" else "")
            for t in cb.triggers]
    if cb.catalysts:
        lines += ["", "## 近期催化剂"] + [f"- {c['when_text']}：{c['event']}（KPI：{c['kpi'] or '—'}）" for c in cb.catalysts]
    if cb.related:
        lines += ["", "## 产业链相关标的"] + [
            f"- {r['name']}（{r['relation']}）：1日 {pct(sn['ret_1d'])}，5日 {pct(sn['ret_5d'])}，20日 {pct(sn['ret_20d'])}"
            for r, sn in cb.related if sn]
    if cb.gaps:
        lines += ["", "## 数据缺口"] + [f"- {g}" for g in cb.gaps]
    try:
        res = app.llm.complete("stock_brief", [STOCK_BRIEF_SYSTEM, app.company_context(c)], "\n".join(lines), max_tokens=8000)
        cb.analysis = res.text.strip()
    except LLMError as e:
        cb.errors.append(f"✗ {c.name} 研判失败：{e}")


# ---------------------------------------------------------------- 渲染

_PCT_RE = re.compile(r"(?<![\w.%])([+-]\d+(?:\.\d+)?%)")


def _colorize(html: str, red_up: bool) -> str:
    """HTML 中给带符号的百分比上色（Markdown 源文件保持干净）。"""
    up, down = ("up", "down") if red_up else ("down", "up")
    return _PCT_RE.sub(lambda m: f'<span class="{up if m.group(1)[0] == "+" else down}">{m.group(1)}</span>', html)


def _quote_table(app: App, cb: CompanyBrief) -> list[str]:
    rows = ["| 上市地 | 日期 | 收盘 | 1日 | 5日 | 20日 | 20日超额 | 趋势 | RSI14 | 量比 | ATR% | 距250日高 | PE(TTM) | PB | 市值 |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for l, s in cb.snaps:
        if not s:
            rows.append(f"| {MARKET_LABEL[l.market]} {l.display} | 无数据 |" + " |" * 13)
            continue
        q = s["quote"]
        rows.append(
            f"| {MARKET_LABEL[l.market]} {l.display} | {s['date'][5:]} | {num(s['close'])} | {pct(s['ret_1d'])} | "
            f"{pct(s['ret_5d'])} | {pct(s['ret_20d'])} | {pct(s.get('excess_20d'))} | {s['trend']} | "
            f"{num(s['rsi14'], 0)} | {num(s['vol_ratio'], 1)} | {pct(s['atr_pct'], sign=False)} | {pct(s['from_high'])} | "
            f"{num(q.get('pe_ttm'), 1)} | {num(q.get('pb'), 2)} | {big_num(q.get('market_cap'))} |"
        )
    if cb.ah:
        rows.append("")
        rows.append(f"AH 溢价（A 股相对 H 股）：**{pct(cb.ah['current'])}**，20 日均值 {pct(cb.ah['avg_20'])}，"
                    f"60 日区间 {pct(cb.ah['min_60'])} ~ {pct(cb.ah['max_60'])}")
    return rows


STATUS_LABEL = {"ok": "正常", "warning": "苗头", "triggered": "已触发"}


def _tracking_block(cb: CompanyBrief) -> list[str]:
    if not cb.thesis:
        return []
    out = [f"**论点跟踪**（{cb.thesis_date} 深度报告）：{cb.thesis}", ""]
    if (t := cb.tracking) and t["enabled"]:
        rev = t["revision"]
        change = (f"，较上次 {rev['prev_target']:,.2f} {pct(rev['target'] / rev['prev_target'] - 1)}"
                  if rev["prev_target"] and rev["kind"] != "baseline" else "")
        out += [f"**{tracker.TARGET_LABEL}**：{t['live_target']:,.2f} {t['currency']}（牛 {rev['bull'] or 0:,.2f} / "
                f"基准 {rev['base'] or 0:,.2f} / 熊 {rev['bear'] or 0:,.2f}）"
                + (f"｜现价 {t['price']:,.2f}，空间 {pct(t['upside'])}" if t["upside"] is not None else "")
                + f"｜最近{tracker.KIND_LABEL[rev['kind']]}（{fmt_local(rev['created_at'])}{change}）：{rev['summary']}"
                + (f"｜论点{tracker.THESIS_STATUS[rev['thesis_status']]}" if rev.get("thesis_status") in tracker.THESIS_STATUS else ""),
                ""]
    alert = [t for t in cb.triggers if t["status"] != "ok"]
    if alert:
        out += ["| 状态 | 证伪条件 | 依据 | 预设动作 |", "|---|---|---|---|"]
        out += [f"| **{STATUS_LABEL[t['status']]}** | {t['description']} | {t['evidence'] or ''} | {ACTION_LABEL[t['action']]} |"
                for t in alert]
        out.append("")
    ok = len(cb.triggers) - len(alert)
    if ok:
        out += [f"其余 {ok} 条证伪条件未触发。", ""]
    if cb.catalysts:
        out += ["近期催化剂：" + "；".join(f"{c['when_text']} {c['event']}" for c in cb.catalysts), ""]
    rel = [(r, sn) for r, sn in cb.related if sn]
    if rel:
        out += ["| 产业链参照 | 关系 | 收盘 | 1日 | 5日 | 20日 |", "|---|---|---|---|---|---|"]
        out += [f"| {r['name']}（{r['code']}） | {r['relation']} | {num(sn['close'])} | {pct(sn['ret_1d'])} | {pct(sn['ret_5d'])} | "
                f"{pct(sn['ret_20d'])} |" for r, sn in rel]
        out.append("")
    return out


def _events_table(events: list[dict]) -> list[str]:
    rows = ["| 时间 | 重要性 | 方向 | 类型 | 摘要 | 来源 | 发布后 |", "|---|---|---|---|---|---|---|"]
    for e in events:
        src = SOURCE_LABEL.get(e["source"], e["source"])
        media = e.get("source_name") if e["kind"] == "news" and e.get("source_name") else ""
        link = f"[{src}{'·' + media if media and media != src else ''}]({e['url']})" if e.get("url") else src
        summary = (e["summary_zh"] or e["title"]).replace("|", "/")
        rows.append(
            f"| {fmt_local(e['published_at'])} | {'★' * (e['materiality'] or 0)} | {DIRECTIONS.get(e['direction'], '—')} | "
            f"{event_label(e)} | {summary} | {link} | {_reaction_text(e.get('reaction'))} |"
        )
    return rows


def market_overview(app: App, companies: list[Company]) -> list[str]:
    markets = [m for m in ("HK", "A", "US") if any(c.listing(m) for c in companies)]
    quotes = [q for m in markets for q in app.watchlist.benchmarks.get(m, [])] + app.watchlist.market_watch
    seen: set[str] = set()
    quotes = [q for q in quotes if not (q.secid in seen or seen.add(q.secid))]
    rows = ["| 名称 | 日期 | 收盘 | 1日 | 5日 | 20日 | 趋势 |", "|---|---|---|---|---|---|---|"]
    for q in quotes:
        s = technical.snapshot(app.bars(q.secid))
        fx = q.secid.split(".")[0] in ("133", "120")  # 汇率多保留几位
        d, pd_ = (4, 2) if fx else (2, 1)
        if s:
            rows.append(f"| {q.name} | {s['date'][5:]} | {num(s['close'], d)} | {pct(s['ret_1d'], pd_)} | "
                        f"{pct(s['ret_5d'], pd_)} | {pct(s['ret_20d'], pd_)} | {s['trend']} |")
        else:
            rows.append(f"| {q.name} | 无数据 | | | | | |")
    return rows


def _fallback_headline(briefs: list[CompanyBrief]) -> str:
    events = sorted(((cb.company, e) for cb in briefs for e in cb.events), key=lambda x: -(x[1]["materiality"] or 0))[:5]
    lines = [f"- {c.name}：{e['summary_zh'] or e['title']}（重要性 {e['materiality']}）" for c, e in events if (e["materiality"] or 0) >= 3]
    return "\n".join(lines) or "- 自选股无重要性≥3的新事件。"


def headline(app: App, briefs: list[CompanyBrief], overview: list[str], use_llm: bool) -> tuple[str, list[str]]:
    if not use_llm or app.llm.unavailable_reason("market_brief"):
        return _fallback_headline(briefs), []
    parts = ["## 市场", *overview, ""]
    for cb in briefs:
        parts.append(f"## {cb.company.title}")
        parts.append(app.market_line(cb.company))
        parts.append(cb.analysis or "\n".join(f"- {_event_line(e)}" for e in cb.events[:5]) or "（无新事件）")
        parts.append("")
    try:
        res = app.llm.complete("market_brief", [HEADLINE_SYSTEM], "\n".join(parts), max_tokens=4000)
        return res.text.strip(), []
    except LLMError as e:
        return _fallback_headline(briefs), [f"✗ 今日要点生成失败：{e}"]


def build(app: App, companies: list[Company], use_llm: bool = True) -> tuple[Path, Path, str, list[str]]:
    """生成简报，返回 (md 路径, html 路径, 推送用摘要, 消息)。"""
    rc = app.settings.report
    since = iso(utcnow() - timedelta(hours=rc.lookback_hours))
    briefs = [gather(app, c, since) for c in companies]
    msgs: list[str] = []

    llm_reason = app.llm.unavailable_reason("stock_brief") if use_llm else "已用 --no-llm 关闭"
    if not llm_reason:
        with ThreadPoolExecutor(max_workers=app.settings.llm.concurrency) as pool:
            list(pool.map(lambda cb: analyze(app, cb), briefs))
        for cb in briefs:
            msgs += cb.errors
    else:
        msgs.append(f"简报不含 LLM 研判：{llm_reason}")

    overview = market_overview(app, companies)
    top, errs = headline(app, briefs, overview, use_llm and not llm_reason)
    msgs += errs

    now = datetime.now(CST)
    title = f"自选股日报 · {now:%Y-%m-%d} {'周' + '一二三四五六日'[now.weekday()]}"
    out = [f"# {title}", "",
           f"> 生成于 {now:%Y-%m-%d %H:%M}（北京时间）· 事件窗口：最近 {rc.lookback_hours} 小时 · "
           f"研判模型：{app.llm.describe('stock_brief') if not llm_reason else '未启用'}", "",
           "## 今日要点", "", top, "", "## 市场概览", "", *overview, "", "## 自选股"]
    for cb in briefs:
        out += ["", f"### {cb.company.title}", "", *_quote_table(app, cb), ""]
        out += _tracking_block(cb)
        if cb.analysis:
            out += [cb.analysis, ""]
        if cb.events:
            out += [f"**最近 {rc.lookback_hours} 小时事件**（重要性≥{rc.min_materiality}）", "", *_events_table(cb.events), ""]
        else:
            out += [f"最近 {rc.lookback_hours} 小时没有重要性≥{rc.min_materiality} 的事件。", ""]
        if cb.untriaged:
            out += [f"另有 {cb.untriaged} 条信息尚未分级（LLM 未配置或分级失败）。", ""]
        if cb.pending_filings:
            out += ["**未分级的公告**", "", "| 时间 | 来源 | 标题 |", "|---|---|---|"]
            out += [f"| {fmt_local(e['published_at'])} | {SOURCE_LABEL.get(e['source'], e['source'])} | "
                    + (f"[{e['title']}]({e['url']})" if e.get("url") else e["title"]) + " |" for e in cb.pending_filings]
            out.append("")
        if cb.earlier:
            out += ["**过去 7 天的重要事件**", "", *_events_table(cb.earlier), ""]
        if cb.manual:
            out += ["**手动补充数据**：" + "；".join(
                f"{m['metric']} {m['period']} = {m['value'] if m['value'] is not None else ''}{m['unit'] or ''}" for m in cb.manual[:6]), ""]

    gaps = [(cb.company, g) for cb in briefs for g in cb.gaps]
    if gaps:
        out += ["## 数据缺口", "", *[f"- {c.name}：{g}" for c, g in gaps], ""]
    docs = [d for c in companies for d in app.db.documents(c.id)]
    if docs:
        out += ["## 已入库资料", "", "| 公司 | 类型 | 期间 | 标题 | 来源 | 字数 |", "|---|---|---|---|---|---|"]
        names = {c.id: c.name for c in companies}
        out += [f"| {names.get(d['company_id'], d['company_id'])} | {DOC_TYPES.get(d['doc_type'], d['doc_type'])} | "
                f"{d['period'] or ''} | {d['title']} | {d['source']} | {d['chars']:,} |" for d in docs]
        out.append("")
    usage = app.db.llm_usage(iso(datetime.combine(today_local(), datetime.min.time(), CST)))
    if usage:
        out += ["## 今日 LLM 用量", "", "| 模型 | 调用 | 输入 tokens | 输出 tokens | 缓存命中 |", "|---|---|---|---|---|"]
        out += [f"| {u['model']} | {u['ok_calls']}/{u['calls']} | {u['input_tokens'] or 0:,} | {u['output_tokens'] or 0:,} | "
                f"{u['cache_read'] or 0:,} |" for u in usage]
        out.append("")
    out += ["---", f"*{DISCLAIMER}*", ""]

    text = "\n".join(out)
    report_dir = app.data_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{now:%Y-%m-%d_%H%M}"
    md_path, html_path = report_dir / f"{stem}.md", report_dir / f"{stem}.html"
    md_path.write_text(text, encoding="utf-8")
    body = _colorize(md.markdown(text, extensions=["tables", "sane_lists"]), rc.color_scheme == "red_up")
    html = HTML_TEMPLATE.format(title=title, body=body)
    html_path.write_text(html, encoding="utf-8")
    (report_dir / "latest.html").write_text(html, encoding="utf-8")

    summary = _push_summary(briefs, top, html_path)
    return md_path, html_path, summary, msgs


def _push_summary(briefs: list[CompanyBrief], top: str, html_path: Path) -> str:
    lines = ["今日要点：", top, "", "个股："]
    for cb in briefs:
        quotes = "，".join(
            f"{l.display} {num(s['close'])}（{pct(s['ret_1d'])}）" for l, s in cb.snaps if s
        )
        top_m = max((e["materiality"] or 0 for e in cb.events), default=0)
        lines.append(f"- {cb.company.name}：{quotes or '无行情'}；事件 {len(cb.events)} 条" + (f"，最高重要性 {top_m}" if top_m else ""))
    lines += ["", f"完整报告：{html_path}"]
    return "\n".join(lines)


# ---------------------------------------------------------------- 重要事件提醒

def alerts(app: App, do_push: bool = True, window_hours: int = 24) -> list[str]:
    min_m = app.settings.notify.alert_min_materiality
    cutoff = utcnow() - timedelta(hours=window_hours)
    pending = app.db.pending_alerts(min_m)
    app.db.mark_alerted([p["uid"] for p in pending if parse_iso(p["published_at"]) < cutoff])
    fresh = [p for p in pending if parse_iso(p["published_at"]) >= cutoff]
    trig = monitor.trigger_alerts(app)
    if not fresh and not trig:
        return [f"没有新的重要事件（重要性≥{min_m}）或触发的证伪条件"]
    lines = [f"[证伪条件触发·{ACTION_LABEL[t['action']]}] {c.name if c else t['company_id']}：{t['description']}（{t['evidence'] or ''}）"
             for t, c in trig]
    for p in fresh:
        c = app.watchlist.get(p["company_id"])
        lines.append(
            f"[重要性{p['materiality']}·{DIRECTIONS.get(p['direction'], '')}] {c.name if c else p['company_id']}："
            f"{p['summary_zh'] or p['title']}（{fmt_local(p['published_at'])} {SOURCE_LABEL.get(p['source'], p['source'])}）"
            + (f"\n  {p['url']}" if p.get("url") else "")
        )
    text = "\n".join(lines)
    msgs = [text]
    if do_push:
        title = f"【重要事件提醒】{len(fresh)} 条" + (f"，证伪条件触发 {len(trig)} 条" if trig else "")
        msgs += notify.push(app.settings, title, text) or ["（未配置推送渠道，仅在终端显示）"]
    app.db.mark_alerted([p["uid"] for p in fresh])
    app.db.mark_trigger_alerted([t["id"] for t, _ in trig])
    return msgs
