"""L1 硬过滤与 L2 代码打分：五个策略桶、粗算赔率、前瞻催化剂、价值陷阱标记与分层。全部由代码计算，不调用模型。

分层（两个池相同的语义，优先级从高到低）：
- A 高胜率 + 高赔率：赔率达标、下行 ≤ max_downside_a、有催化剂、没有严重风险标记
- B 高胜率：稳定现金牛（质量红利桶入选）或「有催化剂 + 下行 ≤ max_downside_a」，赔率 ≥ odds_win，没有严重风险标记
- C 高赔率：赔率 ≥ odds_b（或产业链瓶颈评分高的成长型），胜率证据不足
- D 观察

粗算赔率（只用于排序和分层，深研后以深度报告的情景估值为准）：
- 保守 FCF = TTM FCF 与近 3 年平均 FCF 取低（只有一个时用那一个）
- 合理价值 = 保守 FCF ÷ fair_fcf_yield + 净现金（FCF 不为正时只算打折后的净现金）
- 地板 = max(现金部分 + 最差一期 FCF ÷ bear_fcf_yield, 0)；现金部分 = 净现金 × (1 − cash_haircut)，净负债全额扣减
- 上行 = 合理价值 ÷ 市值 − 1；下行 = max(1 − 地板 ÷ 市值, min_downside)；赔率 = 上行 ÷ 下行
- EV 不到市值 10% 时 FCF Yield 没有意义（分母接近 0），按「EV 接近零」处理
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .. import fcf
from ..config import ScreenConfig
from ..utils import parse_iso, today_local, utcnow
from .calendar import window
from .events import LABEL, NEG_REVISION, REVISION_KINDS
from .universe import age_days

BUCKET_LABEL = {"floor": "现金地板", "quality": "质量红利", "event": "事件催化", "theme": "产业链瓶颈", "revision": "预期修正"}
POOL_LABEL = {"long": "长线池（6–24 个月）", "event": "事件池（1–3 个月）"}
TIER_LABEL = {"A": "A 类：高胜率 + 高赔率", "B": "B 类：高胜率", "C": "C 类：高赔率", "D": "D 类：观察", "X": "快筛淘汰"}
TIER_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3, "X": 4}
CATALYST_DAYS = {"long": 365, "event": 90}   # 前瞻催化剂的时间窗
MAX_ODDS = 20.0


@dataclass
class Ctx:
    cfg: ScreenConfig
    hkdcny: float | None                       # 1 港元 = ? 人民币
    events: dict[str, list[dict]] = field(default_factory=dict)
    themes: dict[str, list[dict]] = field(default_factory=dict)   # secid → [{theme, node, bottleneck}]
    calendar: dict[str, dict] = field(default_factory=dict)       # secid → calendar.build 的结果


# ---------------------------------------------------------------- L1 硬过滤

def hard_filter(u: dict, cfg: ScreenConfig) -> str | None:
    """不通过时返回原因。流动性（20 日均成交额）要日线，放在候选阶段再查。"""
    if not u.get("mcap") or not u.get("price"):
        return "缺少市值或价格"
    if cfg.exclude_st and u.get("st"):
        return "ST"
    if u["mcap"] < cfg.min_mcap.get(u["market"], 0):
        return "市值低于下限"
    days = age_days(u)
    if days is not None and days < cfg.min_listing_days:
        return "上市不满规定天数"
    return None


def fcf_applicable(u: dict, cfg: ScreenConfig) -> bool:
    if (u.get("org_type") or "一般企业") not in ("一般企业", "通用"):
        return False
    ind = u.get("industry") or ""
    return not any(x in ind for x in cfg.fcf_exclude_industries)


def mcap_report_ccy(u: dict, hkdcny: float | None) -> float | None:
    """市值换成报表币种（港股的内地公司多以人民币报账）。"""
    if u["market"] == "A" or u.get("report_ccy") == "HKD":
        return u["mcap"]
    if u.get("report_ccy") == "CNY":
        return u["mcap"] * hkdcny if hkdcny else None
    if u.get("report_ccy") == "USD":
        return u["mcap"] / 7.8
    return None


# ---------------------------------------------------------------- 桶 1：现金地板

def floor_metrics(u: dict, cfg: ScreenConfig, hkdcny: float | None) -> dict | None:
    data = u.get("fcf")
    mcap = mcap_report_ccy(u, hkdcny) if data else None
    if not data or not mcap:
        return None
    m = fcf.metrics(data, mcap)
    ev, nc = m["ev"], m["net_cash"]
    cands = [x for x in (m["fcf_base"], m["fcf_avg3"]) if x is not None]
    cons = min(cands) if cands else None
    ev_small = 0 <= ev < 0.1 * mcap
    y_cons = cons / ev if cons is not None and ev > 0 and not ev_small else None
    cash_part = nc * (1 - cfg.cash_haircut) if nc > 0 else nc
    worst = m["fcf_worst"]
    floor_eq = max(cash_part + (worst / cfg.bear_fcf_yield if worst and worst > 0 else 0.0), 0.0)
    fair_eq = cons / cfg.fair_fcf_yield + nc if cons and cons > 0 else max(cash_part, 0.0)
    upside, downside = fair_eq / mcap - 1, max(1 - floor_eq / mcap, cfg.min_downside)
    odds = min(upside / downside, MAX_ODDS) if upside > 0 else 0.0
    info = u.get("info") or {}
    flags: list[dict] = []

    def flag(code: str, text: str, severity: str) -> None:
        flags.append({"code": code, "text": text, "severity": severity})

    if m["capex_proxy"]:
        flag("capex_proxy", f"{'、'.join(m['capex_proxy'])} 资本开支科目缺失，按投资活动净流出估算（偏保守），需用年报核对", "info")
    if ev_small:
        flag("ev_small", "EV 接近零（净现金接近市值），FCF Yield 不适用", "info")
    if len(m["annual"]) >= 2 and m["fcf_positive_years"] < 2:
        flag("fcf_unstable", f"FCF 不稳定：近 {len(m['annual'])} 个年度只有 {m['fcf_positive_years']} 年为正", "high")
    if m["fcf_base"] is not None and m["fcf_base"] < 0:
        flag("fcf_negative", f"{m['fcf_base_label']} FCF 为负", "mid")
    if m["cash_like"] >= 0.3 * mcap and m["debt"] >= 0.5 * m["cash_like"]:
        flag("cash_and_debt", "存贷双高：现金多、有息负债也多，需核实现金真实性与可动用性", "high")
    dy, payout = info.get("dividend_yield"), info.get("payout")
    if u["market"] == "HK" and (m["negative_ev"] or (m["net_cash_ratio"] or 0) >= cfg.min_net_cash_ratio) and not dy and not payout:
        flag("no_payout", "净现金高但不派息：Negative EV 陷阱风险（现金可能长期不分配）", "mid")
    if m["minority"] > 0.5 * mcap:
        flag("minority", "少数股东权益超过市值一半：净现金有相当部分不归上市公司股东", "mid")
    if (m["net_cash_ratio"] or 0) < -1:
        flag("net_debt", "净负债超过市值", "mid")
    if (today_local() - date.fromisoformat(m["balance_date"])).days > 300:
        flag("stale", f"资产负债表截至 {m['balance_date']}，数据较旧", "mid")
    price = u["price"]
    return {
        "ev": ev, "net_cash": nc, "net_cash_ratio": m["net_cash_ratio"], "negative_ev": m["negative_ev"],
        "yield_base": None if ev_small else m["yield_base"], "yield_avg3": None if ev_small else m["yield_avg3"],
        "yield_cons": y_cons, "ev_small": ev_small, "fcf_base_label": m["fcf_base_label"],
        "fcf_positive_years": m["fcf_positive_years"], "annual_n": len(m["annual"]),
        "annual_fcf": [x["fcf"] for x in m["annual"]], "fcf_base": m["fcf_base"],
        "fcf_cons_mcap": cons / mcap if cons is not None else None,
        "payback_years": fcf.payback_years(ev, cons) if cons is not None else None,
        "balance_date": m["balance_date"], "report_ccy": u.get("report_ccy"),
        "upside": upside, "downside": downside, "odds": odds,
        "fair_price": price * fair_eq / mcap, "floor_price": price * floor_eq / mcap,
        "dividend_yield": dy, "payout": payout, "flags": flags,
    }


def floor_score(fm: dict, cfg: ScreenConfig) -> tuple[bool, float]:
    y, ncr = fm["yield_cons"] or 0.0, fm["net_cash_ratio"] or 0.0
    ok = (fm["negative_ev"] or fm["ev_small"] or ncr >= cfg.min_net_cash_ratio
          or (y >= cfg.min_fcf_yield and (fm["fcf_positive_years"] >= 2 or fm["annual_n"] < 2)))
    high = sum(1 for f in fm["flags"] if f["severity"] == "high")
    mid = sum(1 for f in fm["flags"] if f["severity"] == "mid")
    s = (40 * min(max(y, 0) / 0.25, 1) + 25 * min(max(ncr, 0), 1) + 10 * fm["fcf_positive_years"] / 3
         + 10 * min((fm["dividend_yield"] or 0) / 0.08, 1) + 15 * min(fm["odds"] / 5, 1) - 10 * high - 4 * mid)
    if fm["negative_ev"]:
        s += 10
    elif fm["ev_small"]:
        s += 5
    return ok, round(s, 1)


# ---------------------------------------------------------------- 桶 2 / 桶 4：事件与预期修正

def _age_days(e: dict) -> float:
    return max((utcnow() - parse_iso(e["published_at"])).total_seconds() / 86400, 0)


KIND_CAP = {"buyback": 1.5}  # 大公司天天回购（每天一份翌日披露报表），单靠回购最多记 1.5


def event_summary(evs: list[dict]) -> dict:
    """同一类事件同一天只算一次；强度按 15 天半衰期衰减，回购另有上限。"""
    seen, per_kind, neg, kinds = set(), {}, [], {}
    for e in evs:
        key = (e["kind"], e["published_at"][:10])
        if key in seen:
            continue
        seen.add(key)
        if e["direction"] == "positive":
            if e["kind"] not in REVISION_KINDS:
                per_kind[e["kind"]] = per_kind.get(e["kind"], 0.0) + e["weight"] * 0.5 ** (_age_days(e) / 15)
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        else:
            neg.append(e)
    pos = sum(min(v, KIND_CAP.get(k, 99.0)) for k, v in per_kind.items())
    return {"strength": round(min(pos, 6.0), 2), "kinds": kinds, "negatives": neg,
            "veto": [e for e in neg if e["weight"] >= 2 or e["kind"] in NEG_REVISION]}


def event_score(es: dict, fm: dict | None, u: dict) -> tuple[bool, float]:
    ok = es["strength"] >= 1.0 and not es["veto"]
    support = 1.0 if (fm and (fm["yield_cons"] or 0) >= 0.05) or 0 < (u.get("pe_ttm") or 0) <= 15 else 0.0
    return ok, round(60 * min(es["strength"] / 4, 1) + 20 * support + (20 if not es["negatives"] else 0), 1)


def revision_score(u: dict, evs: list[dict], cfg: ScreenConfig) -> tuple[bool, float, dict]:
    info = u.get("info") or {}
    py, ry = info.get("profit_yoy"), info.get("revenue_yoy")
    gm, gp = info.get("gross_margin"), info.get("gross_margin_prev")
    rd = info.get("report_date")
    fresh = bool(rd) and (today_local() - date.fromisoformat(rd)).days <= 200
    fund_up = fresh and py is not None and ry is not None and py >= cfg.min_profit_growth and ry >= cfg.min_revenue_growth
    horizon = utcnow() - timedelta(days=cfg.forecast_days)
    fc = max((e["weight"] for e in evs if e["kind"] in REVISION_KINDS and parse_iso(e["published_at"]) >= horizon), default=0.0)
    bad = [e for e in evs if e["kind"] in NEG_REVISION and parse_iso(e["published_at"]) >= horizon]
    gd = gm - gp if gm is not None and gp is not None else None
    detail = {"profit_yoy": py, "revenue_yoy": ry, "gross_margin": gm, "gross_margin_delta": gd, "report_date": rd,
              "forecast_weight": fc, "fundamental_up": fund_up}
    ok = (fc > 0 or fund_up) and not bad
    s = (30 * min(max(py or 0, 0) / 100, 1) * fresh + 20 * min(max(ry or 0, 0) / 40, 1) * fresh
         + 15 * min(max(gd or 0, 0) / 5, 1) * fresh + 35 * min(fc / 2, 1))
    return ok, round(s, 1), detail


# ---------------------------------------------------------------- 桶 3：产业链瓶颈

def theme_score(u: dict, memberships: list[dict], fm: dict | None) -> tuple[bool, float]:
    if not memberships:
        return False, 0.0
    info = u.get("info") or {}
    ry, gm, gp = info.get("revenue_yoy"), info.get("gross_margin"), info.get("gross_margin_prev")
    gd = gm - gp if gm is not None and gp is not None else 0.0
    pe = u.get("pe_ttm") or 0
    if fm and (fm["yield_cons"] or 0) >= 0.03 or 0 < pe <= 40:
        guard = 1.0
    elif pe > 80 or pe <= 0:
        guard = 0.0
    else:
        guard = 0.5
    bottleneck = any(m.get("bottleneck") for m in memberships)
    return True, round(35 * bottleneck + 20 * min(max(ry or 0, 0) / 40, 1) + 20 * min(max(gd, 0) / 5, 1) + 25 * guard, 1)


# ---------------------------------------------------------------- 桶 5：质量红利（高胜率）

def quality_score(fm: dict | None, u: dict, cal: dict | None, cfg: ScreenConfig) -> tuple[bool, float, dict]:
    """稳定现金牛：收益主要来自确定的现金回报（股息）与稳定的自由现金流，而不是估值修复或事件。
    条件：近 3 个年度与 TTM 的 FCF 都为正且波动小（最差年度 ≥ 3 年平均 × quality_fcf_stability）、连续派息 ≥ quality_min_div_years 年、
    股息率 ≥ quality_min_dividend_yield 且被 FCF 覆盖、保守 FCF Yield ≥ quality_min_fcf_yield、净负债不超过市值的一半、利润没有大幅下滑。"""
    if not fm:
        return False, 0.0, {}
    div = (cal or {}).get("dividend") or {}
    years = div.get("years") or 0
    dy = fm.get("dividend_yield") or 0.0
    annual = [x for x in fm.get("annual_fcf") or []]
    avg = sum(annual) / len(annual) if annual else None
    stability = min(annual) / avg if annual and avg and avg > 0 else 0.0
    fcf_mcap = fm.get("fcf_cons_mcap") or 0.0
    py = (u.get("info") or {}).get("profit_yoy")
    checks = {
        "fcf_all_positive": len(annual) >= 3 and all(x > 0 for x in annual) and (fm.get("fcf_base") or 0) > 0,
        "fcf_stable": stability >= cfg.quality_fcf_stability,
        "dividend_years": years >= cfg.quality_min_div_years,
        "dividend_yield": dy >= cfg.quality_min_dividend_yield,
        "dividend_covered": fcf_mcap >= dy > 0,
        "fcf_yield": (fm["yield_cons"] or 0) >= cfg.quality_min_fcf_yield or fm["negative_ev"] or fm["ev_small"],
        "leverage": (fm["net_cash_ratio"] or 0) >= -0.5,
        "profit_ok": py is None or py >= -20,
        "no_high_flag": not any(f["severity"] == "high" for f in fm["flags"]),
    }
    detail = {"checks": checks, "stability": round(stability, 2), "dividend_years": years, "dividend_yield": dy}
    ok = all(checks.values())
    score = (30 * min(max(stability, 0), 1) + 25 * min(dy / 0.07, 1) + 20 * min(max(fm["yield_cons"] or 0.15, 0) / 0.12, 1)
             + 15 * min(years / 5, 1) + 10 * ((fm["net_cash_ratio"] or 0) > 0))
    return ok, round(score, 1), detail


# ---------------------------------------------------------------- 汇总与分层

def evaluate(u: dict, ctx: Ctx) -> dict:
    """一只股票在五个桶里的表现（不分池）。"""
    cfg = ctx.cfg
    cal = ctx.calendar.get(u["secid"])
    dy = ((cal or {}).get("dividend") or {}).get("dividend_yield")
    if dy and not (u.get("info") or {}).get("dividend_yield"):  # A 股股息率由分红记录推算
        u = {**u, "info": {**(u.get("info") or {}), "dividend_yield": dy}}
    fm = floor_metrics(u, cfg, ctx.hkdcny) if fcf_applicable(u, cfg) else None
    evs = ctx.events.get(u["secid"], [])
    es = event_summary(evs)
    out = {"u": u, "fm": fm, "es": es, "cal": cal, "buckets": {}, "scores": {}}
    if fm:
        ok, s = floor_score(fm, cfg)
        if ok:
            out["buckets"]["floor"] = s
        out["scores"]["floor"] = s
    ok, s, out["quality"] = quality_score(fm, u, cal, cfg)
    out["scores"]["quality"] = s
    if ok:
        out["buckets"]["quality"] = s
    ok, s = event_score(es, fm, u)
    out["scores"]["event"] = s
    if ok:
        out["buckets"]["event"] = s
    ok, s, rev = revision_score(u, evs, cfg)
    out["revision"] = rev
    out["scores"]["revision"] = s
    if ok:
        out["buckets"]["revision"] = s
    mem = ctx.themes.get(u["secid"], [])
    ok, s = theme_score(u, mem, fm)
    out["themes"] = mem
    if ok:
        out["buckets"]["theme"] = s
        out["scores"]["theme"] = s
    return out


def catalysts(ev: dict, pool: str, cfg: ScreenConfig) -> list[dict]:
    """代码能识别的催化剂：近期正面公告（已发生）+ 前瞻日历（已公告或按规律推算，在池的时间窗内）+ 深度报告核实过的催化剂。
    业绩窗口只有在业绩趋势向上时才算；推算的派息要求股息率达到质量红利的门槛。"""
    out = [{"text": f"{LABEL.get(k, k)}×{n}", "source": "公告", "confirmed": True} for k, n in ev["es"]["kinds"].items()]
    dy = ((ev["fm"] or {}).get("dividend_yield")) or 0
    rev_up = "revision" in ev["buckets"]
    for x in window(ev.get("cal"), CATALYST_DAYS[pool]):
        if x["direction"] != "positive" and not (x["kind"] == "results" and rev_up):
            continue
        if x["kind"] == "dividend" and not x["confirmed"] and dy < cfg.quality_min_dividend_yield:
            continue
        text = x["text"] + ("（业绩趋势向上）" if x["kind"] == "results" else "")
        out.append({"text": text, "source": "深度报告" if x["kind"] == "dossier" else "日历", "confirmed": x["confirmed"],
                    "date": x["date"]})
    return out


def tier_for(pool: str, odds: float | None, down: float | None, cats: list, quality_ok: bool, high: bool, veto: bool,
             theme: float, safe: bool, cfg: ScreenConfig) -> str:
    """见模块说明。事件池另要求估值有安全垫，高赔率门槛用 event_odds_a。"""
    if veto:
        return "D"
    floor_ok = down is not None and down <= cfg.max_downside_a
    odds_hi = odds is not None and odds >= (cfg.odds_a if pool == "long" else cfg.event_odds_a)
    base = not high and (safe or pool == "long")
    if base and odds_hi and floor_ok and cats:
        return "A"
    if base and odds is not None and odds >= cfg.odds_win and (quality_ok or (cats and floor_ok)):
        return "B"
    if (odds is not None and odds >= cfg.odds_b) or (pool == "long" and theme >= 50):
        return "C"
    return "D"


def pool_candidate(ev: dict, pool: str, cfg: ScreenConfig) -> dict | None:
    """某个池里的候选：分数、分层、标记。不属于该池时返回 None。"""
    b, fm, es, u = ev["buckets"], ev["fm"], ev["es"], ev["u"]
    flags = list(fm["flags"]) if fm else []
    for e in es["negatives"]:
        flags.append({"code": f"event:{e['kind']}", "text": f"{LABEL.get(e['kind'], e['kind'])}：{e['title'][:60]}（{e['published_at'][:10]}）",
                      "severity": "high" if e in es["veto"] else "mid"})
    flags += (ev.get("cal") or {}).get("flags") or []
    high = any(f["severity"] == "high" for f in flags)
    cats = catalysts(ev, pool, cfg)
    odds, down, up = (fm["odds"], fm["downside"], fm["upside"]) if fm else (None, None, None)
    safe = True
    if pool == "long":
        mine = {k: v for k, v in b.items() if k in ("floor", "quality", "theme")}
        if not mine:
            return None
        if "floor" not in mine and "quality" not in mine:  # 只因产业链主题入选：当前 FCF 不代表成长股的价值，赔率留给深度报告
            odds = down = up = None
            flags.append({"code": "growth", "text": "成长型候选：粗算赔率不适用（按当前 FCF 估值会严重低估），赔率在深度报告里用情景估值计算",
                          "severity": "info"})
        score = max(mine.values()) + 10 * min(es["strength"] / 2, 1) + 5 * min(len(cats), 2) - (15 if es["veto"] else 0)
    else:
        mine = {k: v for k, v in b.items() if k in ("event", "revision")}
        if not mine:
            return None
        pe = u.get("pe_ttm") or 0
        safe = not high and ((fm is not None and (fm["yield_cons"] or 0) >= 0.03) or 0 < pe <= 40)
        if not safe:
            flags.append({"code": "no_cushion", "text": "估值没有安全垫（FCF Yield < 3% 且 PE 不在 0–40 倍）或有严重风险标记",
                          "severity": "mid"})
        strong = es["strength"] >= 2 or ev["revision"]["forecast_weight"] >= 1.5
        score = max(mine.values()) + (10 if safe else -10) + (5 if strong else 0)
    tier = tier_for(pool, odds, down, cats, "quality" in b, high, bool(es["veto"]), b.get("theme", 0), safe, cfg)
    cat_text = [c["text"] + ("" if c["confirmed"] else "〔推算〕") for c in cats]
    return {"pool": pool, "secid": u["secid"], "market": u["market"], "code": u["code"], "name": u["name"],
            "industry": u.get("industry"), "buckets": ",".join(sorted(mine, key=lambda k: -mine[k])), "score": round(score, 1),
            "tier": tier, "odds": odds, "upside": up, "downside": down, "price": u["price"], "price_date": u.get("quote_date"),
            "currency": u["currency"], "flags": flags, "catalysts": cat_text, "org_code": u.get("org_code"),
            "metrics": {"floor": fm, "quality": ev.get("quality"), "revision": ev["revision"], "event_strength": es["strength"],
                        "scores": ev["scores"], "themes": ev["themes"], "pe_ttm": u.get("pe_ttm"), "pb": u.get("pb"), "mcap": u["mcap"],
                        "info": u.get("info") or {}, "catalysts": cat_text, "catalyst_detail": cats,
                        "calendar": (window(ev.get("cal"), CATALYST_DAYS[pool]) if ev.get("cal") else []),
                        "dividend": ((ev.get("cal") or {}).get("dividend") or {}) | {"past_ex": None}}}


def dedupe_ah(cands: list[dict]) -> list[dict]:
    """A+H 公司两地都入选时只保留分数高的一地，另一地写进标记。"""
    best: dict[str, dict] = {}
    out = []
    for c in sorted(cands, key=lambda c: -c["score"]):
        oc = c.get("org_code")
        if oc and oc in best:
            other = best[oc]
            other["flags"].append({"code": "ah", "text": f"另有{'A 股' if c['market'] == 'A' else '港股'} {c['code']}（分数 {c['score']}）",
                                   "severity": "info"})
            continue
        if oc:
            best[oc] = c
        out.append(c)
    return out


def top(cands: list[dict], n: int) -> list[dict]:
    return sorted(cands, key=lambda c: -c["score"])[:n]
