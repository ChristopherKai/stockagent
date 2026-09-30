"""桶 2（事件催化）与桶 4 的业绩预告：全市场公告标题扫描，按规则（代码）归类，不调用模型。

- 港股：港交所披露易按日取全市场公告（一天一次请求），按公告类别与标题归类。
- A 股：巨潮按关键词全市场检索（关键词只用来缩小范围，归类仍按标题规则重新判断），
  外加东方财富业绩预告（预增 / 扭亏 / 预减 / 首亏 …）。
只保留股票池里的标的。负面事件（非标审计、立案调查、盈利警告、供股、减持…）作为否决项。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable

from ..app import App, _try
from ..sources import cninfo, emscreen, hkex
from ..utils import CST, iso, log, sha1, to_float, to_simplified, today_local, utcnow
from . import store
from .universe import session

WATERMARK = "screen:events:{}"


@dataclass(frozen=True)
class Rule:
    kind: str
    label: str
    direction: str          # positive | negative
    weight: float
    pattern: str            # 在「标题 + 类别」（简体）中匹配
    markets: str = "HK,A"
    exclude: str = ""


RULES = [
    Rule("privatization", "私有化 / 要约收购", "positive", 3.0, r"私有化|全面要约|要约收购|协议安排|收购守则|自愿有条件"),
    Rule("special_dividend", "特别股息 / 特别分红", "positive", 2.0, r"特别股息|特别分红|特别派息|特别现金股息"),
    Rule("spin_off", "分拆上市", "positive", 2.0, r"分拆", exclude=r"股份拆细|拆细"),
    Rule("buyback", "股份回购", "positive", 1.0, r"股份购回|回购", exclude=r"注销|限制性股票|出售已回购|回购义务|回购条款|购回授权|回购报告书的?更正"),
    Rule("insider_buy", "股东 / 高管增持", "positive", 1.5, r"增持", exclude=r"减持|增持计划.*(终止|延期)|终止.*增持"),
    Rule("disposal", "重大资产出售", "positive", 1.5, r"非常重大的出售|重大资产出售|出售.*(主要交易|须予披露)|(主要交易|须予披露).*出售"),
    Rule("restructure", "重大资产重组", "positive", 1.0, r"重大资产重组", exclude=r"终止"),
    Rule("incentive", "股权激励", "positive", 0.8, r"股票期权激励计划|限制性股票激励计划|股权激励计划", markets="A",
         exclude=r"注销|作废|回购注销|终止"),
    Rule("profit_up", "正面盈利预告", "positive", 1.5, r"正面盈利|盈利预喜|盈利预增"),
    Rule("audit", "非标审计 / 更换核数师", "negative", 3.0, r"非标准报告|非标准审计|核数师辞任|更换核数师|变更会计师事务所|无法表示意见|保留意见"),
    Rule("investigation", "立案调查 / 监管处罚", "negative", 3.0, r"立案|行政处罚|纪律处分|监管措施决定"),
    Rule("default", "违约 / 清盘", "negative", 3.0, r"清盘|违约|破产|债务重组"),
    Rule("delist_risk", "退市风险 / 延迟披露", "negative", 2.0, r"退市风险|延迟刊发|延迟发布|取消上市地位|除牌"),
    Rule("profit_warning", "盈利警告", "negative", 1.5, r"盈利警告"),
    Rule("dilution", "供股 / 配售", "negative", 1.0, r"供股|配售新股|根据一般授权发行|认购新股", markets="HK"),
    Rule("reduce", "股东减持", "negative", 1.0, r"减持", markets="A", exclude=r"不减持|终止.*减持|减持.*(完成|结果)"),
]
LABEL = {r.kind: r.label for r in RULES} | {"forecast_up": "业绩预增 / 扭亏", "forecast_down": "业绩预减 / 亏损"}
REVISION_KINDS = {"profit_up", "forecast_up"}               # 计入桶 4（预期修正）
NEG_REVISION = {"profit_warning", "forecast_down"}
CNINFO_KEYWORDS = ["回购", "增持", "特别分红", "要约收购", "分拆", "重大资产出售", "重大资产重组", "激励计划", "立案", "减持",
                   "非标准", "会计师事务所", "退市风险"]
FORECAST_UP = {"预增", "扭亏", "略增", "续盈"}
FORECAST_DOWN = {"预减", "首亏", "续亏", "略减", "增亏"}


def classify(title: str, category: str, market: str) -> Rule | None:
    text = to_simplified(f"{title} {category}")
    for r in RULES:
        if market in r.markets and re.search(r.pattern, text) and not (r.exclude and re.search(r.exclude, text)):
            return r
    return None


def _event(rule: Rule, u: dict, source: str, source_id: str, title: str, category: str, url: str | None,
           published: datetime) -> dict:
    return {"uid": sha1(source, source_id, u["secid"]), "secid": u["secid"], "market": u["market"], "code": u["code"],
            "name": u["name"], "source": source, "kind": rule.kind, "direction": rule.direction, "weight": rule.weight,
            "title": title, "category": category, "url": url, "published_at": iso(published)}


def _days(app: App, key: str) -> list:
    """从上次扫描的日期（含，当天可能还没扫完）到今天；首次回看 event_lookback_days 天。"""
    today = today_local()
    last = app.db.kv_get(WATERMARK.format(key))
    start = max(datetime.fromisoformat(last).date() if last else today - timedelta(days=app.settings.screen.event_lookback_days),
                today - timedelta(days=60))
    return [start + timedelta(days=i) for i in range((today - start).days + 1)]


def scan(app: App, markets: list[str] | None = None, progress: Callable[[str], None] = lambda m: None) -> list[str]:
    markets = markets or app.settings.screen.markets
    uni = {m: {u["code"]: u for u in store.universe(app.db, m)} for m in markets}
    s = session(app)
    msgs = []
    if "HK" in markets and uni["HK"]:
        msgs += _scan_hk(app, s, uni["HK"], progress)
    if "A" in markets and uni["A"]:
        msgs += _scan_a(app, s, uni["A"], progress)
    return msgs


def _scan_hk(app: App, s, uni: dict, progress) -> list[str]:
    events, days = [], _days(app, "hkex")
    progress(f"港交所全市场公告（{days[0]} 起 {len(days)} 天）…")
    done = None
    for d in days:
        rows = _try(app, "hkex", hkex.market_announcements, s, datetime.combine(d, time(12), tzinfo=CST))
        if rows is None:
            break
        done = d
        for x in rows:
            for code in x["codes"]:
                u = uni.get(code)
                rule = classify(x["title"], x["category"], "HK") if u else None
                if rule:
                    events.append(_event(rule, u, "hkex", x["source_id"], to_simplified(x["title"]),
                                         to_simplified(x["category"]), x["url"], x["published_at"]))
    if done:
        app.db.kv_set(WATERMARK.format("hkex"), done.isoformat())
    n = store.add_events(app.db, events)
    return [f"港股公告事件 {len(events)} 条（新增 {n}）" + ("" if done == days[-1] else "（港交所请求失败，下次续扫）")]


def _scan_a(app: App, s, uni: dict, progress) -> list[str]:
    days = _days(app, "cninfo")
    start = datetime.combine(days[0], time(0), tzinfo=CST)
    end = datetime.combine(days[-1], time(23, 59), tzinfo=CST)
    progress(f"巨潮全市场公告关键词检索（{days[0]} 起）…")
    events, ok = [], True
    for kw in CNINFO_KEYWORDS:
        rows = _try(app, "cninfo", cninfo.search, s, kw, start, end)
        if rows is None:
            ok = False
            break
        for x in rows:
            u = uni.get(x["code"] or "")
            rule = classify(x["title"], "", "A") if u else None
            if rule:
                events.append(_event(rule, u, "cninfo", x["source_id"], x["title"], "", x["url"], x["published_at"]))
    if ok:
        app.db.kv_set(WATERMARK.format("cninfo"), days[-1].isoformat())
    n = store.add_events(app.db, events)
    msgs = [f"A 股公告事件 {len(events)} 条（新增 {n}）" + ("" if ok else "（巨潮请求失败，下次续扫）")]
    since = today_local() - timedelta(days=app.settings.screen.forecast_days)
    rows = _try(app, "eastmoney_dc", emscreen.a_forecasts, s, since) or []
    fc = []
    for r in rows:
        u = uni.get(r.get("SECURITY_CODE") or "")
        typ = r.get("PREDICT_TYPE") or ""
        if not u or not r.get("NOTICE_DATE") or "净利润" not in (r.get("PREDICT_FINANCE") or ""):
            continue
        if typ in FORECAST_UP:
            low = to_float(r.get("ADD_AMP_LOWER"))
            w = 2.0 if typ == "扭亏" else min(max((low or 0) / 50, 0.5), 3.0)
            direction, kind = "positive", "forecast_up"
        elif typ in FORECAST_DOWN:
            w, direction, kind = 1.5, "negative", "forecast_down"
        else:
            continue
        rule = Rule(kind, LABEL[kind], direction, w, "")
        title = f"业绩预告（{r['REPORT_DATE'][:10]}）：{typ}" + (f"，净利润同比 {r['ADD_AMP_LOWER']}% ~ {r['ADD_AMP_UPPER']}%"
                                                              if r.get("ADD_AMP_LOWER") is not None else "")
        published = datetime.fromisoformat(r["NOTICE_DATE"][:10]).replace(hour=18, tzinfo=CST)
        fc.append(_event(rule, u, "forecast", f"{r['SECUCODE']}:{r['REPORT_DATE'][:10]}:{typ}", title,
                         (r.get("PREDICT_CONTENT") or "")[:200], None, published))
    n = store.add_events(app.db, fc)
    msgs.append(f"A 股业绩预告 {len(fc)} 条（新增 {n}）")
    log.info("事件扫描完成：%s", "；".join(msgs))
    return msgs


def recent(app: App, days: int | None = None) -> dict[str, list[dict]]:
    """近 N 天的事件，按 secid 分组（新的在前）。"""
    since = iso(utcnow() - timedelta(days=days or app.settings.screen.event_days))
    out: dict[str, list[dict]] = {}
    for e in store.events_since(app.db, since):
        out.setdefault(e["secid"], []).append(e)
    return out
