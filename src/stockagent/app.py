"""运行上下文、数据抓取流程，以及供 LLM 使用的公司背景与行情摘要。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

from . import config, technical
from .config import Company, Listing, Settings, Watchlist
from .db import DB
from .llm import LLM
from .sources import ccass, cninfo, eastmoney, emdata, frankfurter, google_news, hkex, ids, sec, tencent
from .utils import (days_ago, file_lock, fmt_local, iso, log, make_session, normalize_title, parse_iso, pct, num, sha1,
                    to_simplified, today_local, utcnow)

FX_HKDCNH = "133.HKDCNH"
FX_USDCNH = "133.USDCNH"
MARKET_LABEL = {"HK": "港股", "A": "A股", "US": "美股"}
SOURCE_LABEL = {
    "hkex": "港交所", "cninfo": "巨潮", "sec": "SEC", "eastmoney": "东方财富",
    "google_news": "Google新闻", "ima": "ima知识库", "ccass": "港交所CCASS", "manual": "手动",
    "consensus": "一致预期对比",
}


@dataclass
class App:
    settings: Settings
    watchlist: Watchlist
    root: Path
    db: DB
    llm: LLM
    _session: requests.Session | None = field(default=None, repr=False)
    down: set[str] = field(default_factory=set)  # 本次运行中已熔断的数据源

    @classmethod
    def load(cls, config_path: str | Path = "config.yaml") -> "App":
        settings, watchlist, root = config.load(config_path)
        db = DB(settings.data_dir / "stock.db")
        return cls(settings, watchlist, root, db, LLM(settings.llm, db))

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = make_session(self.settings.sources.request_delay)
        return self._session

    @property
    def data_dir(self) -> Path:
        return self.settings.data_dir

    def lock_path(self, name: str) -> Path:
        return self.data_dir / "tmp" / "locks" / f"{name}.lock"

    def lock(self, name: str):
        """进程锁（非阻塞，yield 是否拿到）：dossier 同一时间只跑一个深度报告；pipeline 串行化 run / alert / target run；
        web 保证只有一个调度器与任务执行器。"""
        return file_lock(self.lock_path(name))

    def secid(self, listing: Listing) -> str:
        return ids.em_secid(self.db, self.session, listing)

    def bench_secid(self, listing: Listing) -> str | None:
        if listing.benchmark:
            return listing.benchmark
        q = self.watchlist.benchmarks.get(listing.market)
        return q[0].secid if q else None

    def bars(self, secid: str | None, limit: int = 400) -> pd.DataFrame | None:
        if not secid:
            return None
        df = self.db.bars(secid, limit)
        return df if not df.empty else None

    # ------------------------------------------------------------ 供 LLM 使用的背景

    def listing_snapshot(self, listing: Listing) -> dict | None:
        secid = self.secid(listing)
        snap = technical.snapshot(self.bars(secid), self.bars(self.bench_secid(listing)))
        if snap:
            snap["quote"] = self.db.quote(secid) or {}
            snap["secid"] = secid
        return snap

    def ah_premium(self, company: Company) -> dict | None:
        a, h = company.listing("A"), company.listing("HK")
        if not (a and h):
            return None
        return technical.ah_premium(self.bars(self.secid(a)), self.bars(self.secid(h)), self.bars(FX_HKDCNH))

    def ads_premium(self, company: Company) -> dict | None:
        """美股 ADS 相对港股的溢价（需要在观察清单里填写美股上市地的 ads_ratio）。"""
        us, hk = company.listing("US"), company.listing("HK")
        if not (us and hk and us.ads_ratio):
            return None
        return technical.adr_premium(self.bars(self.secid(us)), self.bars(self.secid(hk)), self.bars(FX_USDCNH),
                                     self.bars(FX_HKDCNH), us.ads_ratio)

    def cross_premiums(self, company: Company) -> list[tuple[str, dict]]:
        """同一家公司不同上市地之间的价差：[(标签, 溢价统计)]。"""
        out = []
        if ah := self.ah_premium(company):
            out.append(("AH溢价（A相对H）", ah))
        if ads := self.ads_premium(company):
            out.append(("ADS溢价（美股相对港股）", ads))
        return out

    def market_line(self, company: Company) -> str:
        parts = []
        pricing = company.pricing_listing.key if len(company.listings) > 1 else None
        for l in company.listings:
            s = self.listing_snapshot(l)
            tag = "（定价上市地）" if l.key == pricing else ""
            if not s:
                parts.append(f"{MARKET_LABEL[l.market]} {l.display}{tag}：暂无行情")
                continue
            q = s["quote"]
            parts.append(
                f"{MARKET_LABEL[l.market]} {l.display}{tag}（{s['date']}）：收 {num(s['close'])} {l.currency}，"
                f"1日 {pct(s['ret_1d'])}，5日 {pct(s['ret_5d'])}，20日 {pct(s['ret_20d'])}，"
                f"20日超额 {pct(s.get('excess_20d'))}，趋势 {s['trend']}，RSI14 {num(s['rsi14'], 0)}，"
                f"量比 {num(s['vol_ratio'], 1)}，距250日高点 {pct(s['from_high'])}"
                + (f"，PE(TTM) {num(q.get('pe_ttm'), 1)}，PB {num(q.get('pb'), 2)}" if q else "")
                + (f"，南向持股 {q['southbound_ratio']:.2f}%（20日 {q['southbound_chg_20d']:+.2f} 个百分点）"
                   if q.get("southbound_ratio") is not None and q.get("southbound_chg_20d") is not None else "")
            )
        for label, p in self.cross_premiums(company):
            parts.append(f"{label}：{pct(p['current'])}，20日均值 {pct(p['avg_20'])}，"
                         f"60日区间 {pct(p['min_60'])} ~ {pct(p['max_60'])}")
        return "\n".join(parts)

    def fx_line(self) -> str:
        """最新汇率（来自行情库），供 LLM 换算币种，避免模型凭记忆使用过时汇率。"""
        parts = []
        for secid, label in ((FX_HKDCNH, "港元"), (FX_USDCNH, "美元")):
            bars = self.bars(secid)
            if bars is not None:
                rate = float(bars["close"].iloc[-1])
                parts.append(f"1 {label} = {rate:.4f} 人民币（1 人民币 = {1 / rate:.4f} {label}，{bars['date'].iloc[-1]}）")
        return "最新汇率：" + "；".join(parts) if parts else "最新汇率：暂无数据（请用 web_search 查询当日汇率）"

    def company_context(self, company: Company) -> str:
        lines = [f"## 公司：{company.title}"]
        if company.sector:
            lines.append(f"- 行业：{company.sector}")
        lines.append("- 上市：" + "；".join(f"{MARKET_LABEL[l.market]} {l.display}" for l in company.listings)
                     + (f"（定价上市地：{company.pricing_listing.display}）" if len(company.listings) > 1 else ""))
        if company.notes:
            lines.append(f"- 研究备注：{company.notes.strip()}")
        if company.watch_items:
            lines.append("- 重点跟踪：" + "；".join(company.watch_items))
        d = self.db.latest_dossier(company.id)
        if d:
            card = d["card"]
            lines += ["", f"### 投资论点（{d['created_at'][:10]} 深度报告）", f"- 核心判断：{card['thesis']}"]
            if card.get("kill_criteria"):
                lines.append("- 证伪条件：" + "；".join(k["description"] for k in card["kill_criteria"]))
            if card.get("related_tickers"):
                lines.append("- 产业链相关公司：" + "；".join(f"{r['name']}（{r['relation']}）" for r in card["related_tickers"]))
            if card.get("news_keywords"):
                lines.append("- 产业链跟踪词：" + "、".join(card["news_keywords"]))
        prof = self.db.profile(company.id)
        if prof:
            lines += ["", f"### 公司画像（{fmt_local(prof['updated_at'], with_time=False)} 基于年报等资料生成）",
                      prof["profile_md"]]
        else:
            lines.append("- 公司画像：尚未生成（缺少年报等资料或尚未运行 stock profile）")
        return "\n".join(lines)


# ---------------------------------------------------------------- 抓取

def _item(company: Company, listing: Listing | None, kind: str, source: str, x: dict, source_name: str | None) -> dict:
    return {
        "uid": sha1(company.id, source, x.get("source_id") or x.get("url") or x["title"]),
        "company_id": company.id,
        "listing": listing.key if listing else None,
        "kind": kind,
        "source": source,
        "source_name": source_name,
        "title": x["title"],
        "title_norm": normalize_title(x["title"]),
        "snippet": x.get("snippet") or x.get("category") or "",
        "url": x.get("url"),
        "published_at": iso(x["published_at"]),
        "raw_json": json.dumps(x, ensure_ascii=False, default=str),
    }


def _guard(label: str, fn, *args) -> list[str]:
    try:
        return fn(*args) or []
    except Exception as e:  # 单个数据源失败不影响其他
        log.warning("%s 失败：%s", label, e)
        return [f"✗ {label}：{type(e).__name__}: {str(e)[:160]}"]


def fetch(app: App, companies: list[Company], *, prices: bool = True, filings: bool = True,
          news: bool = True, market: bool = True, ima: bool = True) -> list[str]:
    msgs: list[str] = []
    for c in companies:
        for l in c.listings:
            if prices:
                msgs += _guard(f"{c.name} {l.display} 行情", fetch_prices, app, l)
            if filings:
                msgs += _guard(f"{c.name} {l.display} 公告", fetch_filings, app, c, l)
        if news:
            msgs += _guard(f"{c.name} 新闻", fetch_news, app, c)
    if news and ima:
        from . import imafeed  # imafeed 依赖本模块

        msgs += _guard("ima 知识库", imafeed.fetch, app, companies)
    if market and prices:
        msgs += _guard("市场行情", fetch_market, app, companies)
        if app.settings.sources.ccass:  # 日终数据，每天一次；盘中 alert（market=False）不查
            for c in companies:
                for l in c.listings:
                    if l.market == "HK":
                        msgs += _guard(f"{c.name} {l.display} CCASS", fetch_ccass, app, c, l)
    return msgs


PRICE_SOURCES = {"eastmoney": eastmoney, "tencent": tencent}


DOWN_MINUTES = 60  # 数据源被封或连接失败后，多久内不再尝试


def _is_down(app: App, source: str) -> bool:
    if source in app.down:
        return True
    since = app.db.kv_get(f"down:{source}")
    if since and utcnow() - parse_iso(since) < timedelta(minutes=DOWN_MINUTES):
        app.down.add(source)
        return True
    return False


def _try(app: App, source: str, fn, *args, **kwargs):
    """调用某个数据源；连接被拒时熔断该源（本次运行及之后 DOWN_MINUTES 分钟内跳过），避免每个请求都卡在重试上。"""
    if _is_down(app, source):
        return None
    try:
        return fn(*args, **kwargs)
    except (requests.ConnectionError, requests.exceptions.RetryError) as e:
        app.down.add(source)
        app.db.kv_set(f"down:{source}", iso(utcnow()))
        log.warning("%s 连接失败，%d 分钟内不再使用：%s", source, DOWN_MINUTES, str(e)[:120])
    except requests.RequestException as e:
        log.info("%s 请求失败：%s", source, str(e)[:120])
    return None


def klines(app: App, secid: str, limit: int) -> tuple[list[dict], str | None]:
    """按 sources.price_sources 顺序取日线，汇率另有 Frankfurter 兜底。返回 (日线, 来源)。"""
    for name in app.settings.sources.price_sources:
        bars = _try(app, name, PRICE_SOURCES[name].fetch_klines, app.session, secid, limit=limit)
        if bars:
            return bars, name
    if frankfurter.pair_for(secid):
        bars = _try(app, "frankfurter", frankfurter.fetch_klines, app.session, secid, days=int(limit * 1.5))
        if bars:
            return bars, "frankfurter"
    return [], None


def fetch_prices(app: App, listing: Listing) -> list[str]:
    secid = app.secid(listing)
    bars, source = klines(app, secid, app.settings.sources.price_history_days)
    if not bars:
        return [f"✗ {listing.display} 没有取到日线（{secid}，已尝试 {', '.join(app.settings.sources.price_sources)}）"]
    app.db.upsert_bars(secid, bars)
    quote = None
    for name in app.settings.sources.price_sources:
        quote = _try(app, name, PRICE_SOURCES[name].fetch_quote, app.session, secid)
        if quote:
            break
    if listing.market == "HK":  # 港股通持股（南向资金）：记录占比与 20 日变化
        sb = _try(app, "eastmoney_dc", emdata.southbound_holdings, app.session, listing.code)
        summary = emdata.southbound_summary(sb) if sb else None
        if summary:
            quote = {**(quote or {}), "southbound_ratio": summary["ratio"], "southbound_chg_20d": summary.get("chg_ratio_20"),
                     "southbound_date": summary["date"]}
    if quote:
        app.db.save_quote(secid, quote)
    return [f"{listing.display} 日线 {len(bars)} 根，最新 {bars[-1]['date']}（{source}）"]


def fetch_filings(app: App, company: Company, listing: Listing) -> list[str]:
    src = app.settings.sources
    key = f"fetched:filings:{listing.key}"
    last = app.db.kv_get(key)
    start = parse_iso(last) - timedelta(days=2) if last else days_ago(src.announcements_lookback_days)
    end = utcnow()
    if listing.market == "HK":
        sid = ids.hkex_stock_id(app.db, app.session, listing)
        if not sid:
            return [f"✗ 港交所找不到 {listing.code}"]
        rows, source = hkex.announcements(app.session, sid, start, end), "hkex"
    elif listing.market == "A":
        org = ids.cninfo_org_id(app.db, app.session, listing)
        if not org:
            return [f"✗ 巨潮找不到 {listing.code}"]
        rows, source = cninfo.announcements(app.session, listing, org, start, end), "cninfo"
    else:
        if not src.sec_user_agent:
            return [f"跳过 {listing.display} 的 SEC 文件：config.yaml 未设置 sources.sec_user_agent"]
        cik = ids.sec_cik(app.db, app.session, listing, src.sec_user_agent)
        if not cik:
            return [f"✗ SEC 找不到 {listing.code}"]
        rows, source = sec.filings(app.session, cik, src.sec_user_agent, start, set(src.sec_forms)), "sec"
    new = sum(
        app.db.add_item(_item(company, listing, "announcement", source, x, x.get("category") or SOURCE_LABEL[source]))
        for x in rows
    )
    app.db.kv_set(key, iso(end))
    return [f"{listing.display} 公告 {len(rows)} 条（新增 {new}）"]


def ccass_snapshot(app: App, code: str, day) -> dict | None:
    """库里有 day 当日或之前 3 天内的快照就用，否则向港交所查询并保存。"""
    have = app.db.ccass(code, day.isoformat())
    if have and (day - date.fromisoformat(have["date"])).days <= 3:
        return have
    snap = _try(app, "hkex_ccass", ccass.holdings, app.session, code, day)
    if snap and snap.get("date"):
        app.db.save_ccass(code, snap, app.settings.sources.ccass_keep)
        return app.db.ccass(code, snap["date"])
    return None


def ccass_move_text(moves: list[dict]) -> str:
    return "；".join(f"{to_simplified(m['name'])} {m['delta'] / 1e4:+,.0f} 万股（{m['pct']:+.2f} 个百分点，现占 {m['after_pct']:.2f}%）"
                    for m in moves)


def fetch_ccass(app: App, company: Company, listing: Listing) -> list[str]:
    """记录前一交易日的 CCASS 持股；与上一份快照相比有参与者持股变化 ≥ ccass_alert_pct 时，生成一条数据异动进入分级。
    第一次只存快照不比较。"""
    key = f"ccass_checked:{listing.key}"
    today = today_local()
    if app.db.kv_get(key) == today.isoformat():
        return []
    code = listing.code.zfill(5)
    prev = app.db.ccass(code)
    snap = _try(app, "hkex_ccass", ccass.holdings, app.session, code, today - timedelta(days=1))
    if snap is None:
        return [] if "hkex_ccass" in app.down else [f"✗ {listing.display} 没有 CCASS 持股数据"]
    app.db.kv_set(key, today.isoformat())
    if prev and prev["date"] >= snap["date"]:
        return []
    app.db.save_ccass(code, snap, app.settings.sources.ccass_keep)
    if not prev:
        return [f"{listing.display} CCASS 持股快照 {snap['date']}（首次记录，之后逐日比较）"]
    moves = ccass.changes(prev, app.db.ccass(code, snap["date"]), app.settings.sources.ccass_alert_pct)
    if not moves:
        return [f"{listing.display} CCASS {prev['date']} → {snap['date']} 无大额变动"]
    period = f"{prev['date'][5:]}→{snap['date'][5:]}"
    x = {"source_id": f"{code}:{snap['date']}",
         "title": f"CCASS 持股变动（{period}）：" + "、".join(f"{to_simplified(m['name'])} {m['pct']:+.2f}%" for m in moves[:3]),
         "snippet": (f"港交所中央结算系统持股，{prev['date']} 至 {snap['date']}，占已发行股份的变化：{ccass_move_text(moves)}。"
                     "一增一减、数量相近时多为参与者之间转仓（换托管行、配售交收、质押），不一定是买卖；单边大增或大减更值得注意。"),
         "url": ccass.URL, "published_at": utcnow()}
    app.db.add_item(_item(company, listing, "data", "ccass", x, "CCASS 持股"))
    return [f"{listing.display} CCASS 持股变动 {len(moves)} 个参与者（{period}）"]


def _blocked(x: dict, blocked_sources: list[str], patterns: list[re.Pattern]) -> bool:
    media = (x.get("media") or "").lower()
    if any(b.lower() in media for b in blocked_sources):
        return True
    return any(p.search(x["title"]) for p in patterns)


def fetch_news(app: App, company: Company) -> list[str]:
    src = app.settings.sources
    cutoff = days_ago(src.news_lookback_days)
    patterns = [re.compile(p, re.I) for p in src.blocked_title_patterns]
    rows: list[tuple[str, dict]] = []
    errors: list[str] = []
    if src.eastmoney_news:
        for kw in [company.name] + [l.code for l in company.listings if l.market == "A"]:
            found = _try(app, "eastmoney_news", eastmoney.search_news, app.session, kw, src.eastmoney_news_page_size)
            if found is None and "eastmoney_news" in app.down:
                errors.append("✗ 东方财富新闻暂不可用（连接被拒）")
                break
            rows += [("eastmoney", x) for x in found or []]
    if src.google_news:
        for loc in src.google_news_locales:
            for q in company.queries(loc):
                found = _try(app, "google_news", google_news.search, app.session, q, loc, src.news_lookback_days)
                if found is None and "google_news" in app.down:
                    errors.append("✗ Google 新闻不可用（在中国大陆需要能访问 Google，或在 config.yaml 关闭 google_news）")
                    break
                found = sorted(found or [], key=lambda x: x["published_at"], reverse=True)[: src.google_news_max_per_query]
                rows += [("google_news", x) for x in found]
    plan = app.db.plan(company.id) or {}
    for kw in (plan.get("news_keywords") or [])[: src.plan_keywords_max]:
        tag = f"[跟踪词：{kw}] "
        if src.eastmoney_news and "eastmoney_news" not in app.down:
            found = _try(app, "eastmoney_news", eastmoney.search_news, app.session, kw, 10) or []
            rows += [("eastmoney", {**x, "snippet": tag + x.get("snippet", "")}) for x in found]
        if src.google_news and "google_news" not in app.down:
            found = _try(app, "google_news", google_news.search, app.session, kw, "zh-CN", src.news_lookback_days) or []
            found = sorted(found, key=lambda x: x["published_at"], reverse=True)[:15]
            rows += [("google_news", {**x, "snippet": tag}) for x in found]
    kept = new = 0
    for source, x in rows:
        if x["published_at"] < cutoff or _blocked(x, src.blocked_news_sources, patterns):
            continue
        if len(normalize_title(x["title"])) < 6:  # 如"新浪网·"之类的残缺标题
            continue
        kept += 1
        new += app.db.add_item(_item(company, None, "news", source, x, x.get("media")))
    return [f"{company.name} 新闻 {kept} 条（新增 {new}）", *errors]


def fetch_market(app: App, companies: list[Company]) -> list[str]:
    markets = {l.market for c in companies for l in c.listings}
    secids = {q.secid for m in markets for q in app.watchlist.benchmarks.get(m, [])}
    secids |= {q.secid for q in app.watchlist.market_watch}
    secids |= {l.benchmark for c in companies for l in c.listings if l.benchmark}
    for c in companies:
        secids |= {r["secid"] for r in (app.db.plan(c.id) or {}).get("related_tickers", []) if r.get("secid")}
    secids |= {FX_HKDCNH, FX_USDCNH}  # AH 溢价与研究中的币种换算都要用
    failed = []
    for sid in sorted(secids):
        bars, _ = klines(app, sid, 260)
        if bars:
            app.db.upsert_bars(sid, bars)
        else:
            failed.append(sid)
    return [f"市场行情 {len(secids) - len(failed)}/{len(secids)} 个"] + ([f"✗ 未取到：{', '.join(failed)}"] if failed else [])


def add_manual_item(app: App, company: Company, title: str, detail: str = "", url: str | None = None) -> str:
    """手动录入一条消息（如渠道调研、群里看到的传闻），会和新闻一样进入分级。"""
    now = utcnow()
    x = {"source_id": sha1(title, iso(now)), "title": title, "snippet": detail, "url": url, "published_at": now}
    app.db.add_item(_item(company, None, "manual", "manual", x, "手动录入"))
    return f"已录入：{company.name} · {title}"
