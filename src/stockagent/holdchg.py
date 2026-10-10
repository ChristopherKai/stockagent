"""大股东 / 董监高持股变动：港股取港交所权益披露（DI），美股取 Form 4 内部人交易与 Schedule 13D / 13G。

和 consensus、ccass 一样是「程序算好的事实」：数字全部来自申报表本身，不经模型；达到阈值的变动写成 `items`（kind data，source holdchg，
摘要以 `[持股变动]` 开头），交给分级判断含义（买入 / 减持 / 质押各自的方向和轻重由分级、追踪器去读）。随 `stock run` / `stock fetch` 运行，不在盘中 alert 里查。

状态记在 kv `holdchg:<上市地>`（JSON）：`since` 水位线、`done` 已处理的申报编号、`acc` 每个申报人自上次报告以来的累计（净买卖百分点、有方向的申报数、净买卖金额）。
港股按每份申报「涉及股数 ÷ 已发行股份」带方向累计，累计 ≥ 阈值，或连续多份同向申报（控股股东每天小额增持）攒到阈值一半，才报一次并清零：BlackRock 之类被动基金几乎每天一份 ±0.01% 的借还申报，
证券借贷、质押记 0，不会攒出假信号；只算明确的买入 / 卖出 / 获配 / 配售；「其他事项」（A/B 类股转换、内部重组、IPO 前后的持股安排）、担保权益、受赠继承都记 0。不用「申报后比例」相减，因为双重股权公司的比例是按各类股份分别算的。
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from .app import App, _item, _try
from .config import Company, Listing
from .sources import hkdi, ids, nasdaq, sec
from .utils import CST, iso, log, num, today_local, utcnow

TAG = "[持股变动]"
SOURCE = "holdchg"
OVERLAP_DAYS = 5     # 每次从水位线再往回看几天：申报常晚几天才公布，已处理的靠 done 去重
DONE_KEEP = 600      # state.done 最多保存多少个申报编号
HK_URL = "https://di.hkex.com.hk/di/NSSrchCorpList.aspx?sa1=cl&sc={code}&src=MAIN&lang=EN"
ROLE = {"director": "董事/最高行政人员", "holder": "大股东"}
SIDE_ZH = {hkdi.KIND_BUY: "买入", hkdi.KIND_SELL: "卖出", hkdi.KIND_PLEDGE: "质押相关", hkdi.KIND_LEND: "证券借贷", hkdi.KIND_OTHER: "其他"}


def _load(app: App, key: str) -> dict:
    try:
        return json.loads(app.db.kv_get(key) or "{}")
    except ValueError:
        return {}


def _save(app: App, key: str, st: dict) -> None:
    st["done"] = list(st.get("done", []))[-DONE_KEEP:]
    app.db.kv_set(key, json.dumps(st, ensure_ascii=False))


def fetch(app: App, companies: list[Company]) -> list[str]:
    msgs: list[str] = []
    for c in companies:
        for l in c.listings:
            if l.market not in ("HK", "US"):
                continue
            try:
                msgs += (fetch_hk if l.market == "HK" else fetch_us)(app, c, l)
            except Exception as e:  # 单家公司失败不影响其他
                log.warning("%s %s 持股变动失败：%s", c.name, l.display, e)
                msgs.append(f"✗ {c.name} {l.display} 持股变动：{type(e).__name__}: {str(e)[:160]}")
    return msgs


def _start(app: App, st: dict, today: date) -> date:
    if st.get("since"):
        return date.fromisoformat(st["since"]) - timedelta(days=OVERLAP_DAYS)
    return today - timedelta(days=app.settings.sources.holdchg_lookback_days)


def _ref_price(app: App, listing: Listing) -> float | None:
    """最近一根日线收盘价（库里没有就 None，不联网）。"""
    bars = app.bars(app.secid(listing), limit=3)
    return float(bars["close"].iloc[-1]) if bars is not None and len(bars) else None


def _filed(serial: str, fallback: str) -> datetime:
    """申报编号 `DA20260819E00389` 里的申报日 → 当天 18:00（北京时间），不晚于现在；解析不了用事件日。"""
    m = re.search(r"(\d{4})(\d{2})(\d{2})", serial)
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else date.fromisoformat(fallback)
    except ValueError:
        d = date.fromisoformat(fallback)
    return min(datetime(d.year, d.month, d.day, 18, tzinfo=CST), utcnow())


# ---------------------------------------------------------------- 港股

def issued_shares(n: dict) -> float | None:
    """已发行股份数 ≈ 申报后好仓股数 ÷ 比例（比例只有两位小数，小持仓会很不准，< 0.05% 的不用）。"""
    after, pct = n["after"].get("L"), n["pct"].get("L")
    return after / (pct / 100) if after and pct and pct >= 0.05 else None


def _kinds(n: dict) -> set[str]:
    return {hkdi.reason_kind(c) for c in n["reasons"]} or {hkdi.KIND_OTHER}


def _trade_value(n: dict, ref_price: float | None) -> tuple[float | None, str]:
    """一份申报的买卖金额（港元）与价格来源说明；价格非港元或缺失用最近收盘价估算。"""
    shares = n["involved"].get("L")
    if not shares:
        return None, ""
    if n["price"] and (n["currency"] or "HKD") == "HKD":
        return shares * n["price"], f"成交价 HKD {n['price']:,.4f}"
    if ref_price:
        return shares * ref_price, f"按最近收盘价 {ref_price:,.2f} 估算"
    return None, ""


def _sign(code: str) -> int:
    """理由代码对持股的方向：只有明确的买入 / 获配（+1）和卖出 / 配售（-1）才计。
    其他一律记 0：证券借贷、质押、担保权益的取得与解除（贝莱德这类机构常做抵押品方）、受赠 / 继承、衍生工具、
    「其他事项」（A/B 类股转换、内部重组、IPO 前后的持股安排都用它，涉及股数可能很大却不是买卖）。"""
    return hkdi.reason_kind(code) == hkdi.KIND_BUY and 1 or hkdi.reason_kind(code) == hkdi.KIND_SELL and -1 or 0


def implied_change(n: dict) -> float:
    """一份申报隐含的好仓变化（占已发行股份的百分点，带方向）；比例太小算不出已发行股数时记 0。"""
    sign = {s for s in map(_sign, n["reasons"]) if s}  # 同一份申报里不带方向的代码（借贷、其他）忽略
    iss = issued_shares(n)
    if len(sign) != 1 or not iss or not n["involved"].get("L"):
        return 0.0
    return sign.pop() * n["involved"]["L"] / iss * 100


def add_acc(acc: dict, ns: list[dict], ref_price: float | None) -> dict:
    """把这批申报累计进该申报人的状态：pct 净买卖（带方向的百分点）、gross 总买卖（绝对值）、n 有方向的申报数、val 净买卖金额（港元，带方向）。"""
    pct, gross, n, val = (acc.get(k, 0) for k in ("pct", "gross", "n", "val"))
    for x in ns:
        if c := implied_change(x):
            pct, gross, n = pct + c, gross + abs(c), n + 1
            v = _trade_value(x, ref_price)[0]
            val += (v or 0) * (1 if c > 0 else -1)
    return {"pct": round(pct, 4), "gross": round(gross, 4), "n": n, "val": round(val)}


def judge_hk(ns: list[dict], acc: dict, cfg_pct: float, cfg_value: float, ref_price: float | None, streak: int = 5) -> dict | None:
    """同一申报人这批新申报（按事件日升序）→ 要不要报、为什么。acc 是自上次报告以来的累计状态（add_acc）。
    返回 None 表示不报，否则 {"why", "acc"（含这批的累计）, "after"（最新好仓比例）, "value"}。规则（任一成立）：
    ① 累计净买卖 ≥ cfg_pct；② 连续买入 / 卖出：有方向的申报 ≥ streak 份，方向基本一致（净额 ≥ 总额 80%），净买卖 ≥ cfg_pct 的一半——
    控股股东每天小额增持、单日都不起眼，连起来是明确信号；③ 董事 / 最高行政人员同方向买卖金额 ≥ cfg_value；
    ④ 质押类申报涉及股份 ≥ cfg_pct 个百分点已发行股份，或申报人是董事。"""
    last = ns[-1]
    total = add_acc(acc, ns, ref_price)
    why: list[str] = []
    if abs(total["pct"]) >= cfg_pct:
        why.append("drift")
    elif total["n"] >= streak and abs(total["pct"]) >= cfg_pct / 2 and abs(total["pct"]) >= 0.8 * total["gross"]:
        why.append("streak")
    value: dict[str, float] = {}
    if last["kind"] == "director":
        for side in (hkdi.KIND_BUY, hkdi.KIND_SELL):
            vals = [_trade_value(n, ref_price)[0] for n in ns if side in _kinds(n)]
            if vals and (amount := sum(v for v in vals if v)) >= cfg_value:
                value[side] = amount
        if value:
            why.append("value")
    pledged = [n for n in ns if hkdi.KIND_PLEDGE in _kinds(n)]
    if pledged:
        big = any((iss := issued_shares(n)) and (n["involved"].get("L") or 0) / iss * 100 >= cfg_pct for n in pledged)
        if big or last["kind"] == "director":
            why.append("pledge")
    return {"why": why, "acc": total, "after": last["pct"].get("L"), "value": value} if why else None


def _notice_line(n: dict, ref_price: float | None) -> str:
    reasons = "、".join(dict.fromkeys(hkdi.reason_label(c) + f"（{c}）" for c in n["reasons"])) or "未注明"
    parts = [n["date"], reasons]
    if n["involved"].get("L"):
        parts.append(f"涉及 {n['involved']['L']:,.0f} 股")
    if n["price"]:
        parts.append(f"成交价 {n['currency'] or ''} {n['price']:,.4f}".replace("  ", " "))
    if n["after"].get("L") is not None:
        parts.append(f"申报后好仓 {n['after']['L']:,.0f} 股（{num(n['pct'].get('L'))}%）")
    if n["after"].get("S"):
        parts.append(f"淡仓 {n['after']['S']:,.0f} 股（{num(n['pct'].get('S'))}%）")
    parts.append(f"表格 {n['serial']}")
    return "- " + "｜".join(parts)


def hk_item(company: Company, listing: Listing, name: str, ns: list[dict], res: dict, ref_price: float | None) -> dict:
    last = ns[-1]
    role = ROLE[last["kind"]]
    facts = []
    if "value" in res["why"]:
        for side, total in res["value"].items():
            shares = sum(n["involved"].get("L") or 0 for n in ns if side in _kinds(n))
            facts.append(f"{SIDE_ZH[side]} {shares:,.0f} 股（约 HKD {total / 1e4:,.0f} 万）")
    if {"drift", "streak"} & set(res["why"]):
        acc = res["acc"]
        word = "买入" if acc["pct"] > 0 else "卖出"
        facts.append((f"连续 {acc['n']} 份申报" if "streak" in res["why"] else "累计") + f"净{word}约 {abs(acc['pct']):.2f} 个百分点已发行股份"
                      + (f"（约 HKD {abs(acc['val']) / 1e4:,.0f} 万）" if acc["val"] else "")
                      + (f"，好仓现 {num(res['after'])}%" if res["after"] is not None else ""))
    if "pledge" in res["why"] and not facts:
        labels = dict.fromkeys(hkdi.reason_label(c) for n in ns for c in n["reasons"] if hkdi.reason_kind(c) == hkdi.KIND_PLEDGE)
        facts.append("、".join(labels))
    title = f"{role} {name}：" + "；".join(facts)
    lines = [f"{TAG} 港交所权益披露（DI），{listing.display}。{role} {name} 的申报（按事件日升序，好仓比例为占已发行股份）："]
    lines += [_notice_line(n, ref_price) for n in ns[-8:]]
    if len(ns) > 8:
        lines.append(f"（另有 {len(ns) - 8} 份更早的申报未列出）")
    notes = ["同一批股份常经多个受控法团（母公司、中间控股公司）各自申报一次，同一笔买卖可能在不同申报人名下重复出现，不要相加；好仓比例含衍生工具权益与借出股份；买入 / 卖出以申报的理由代码为准，质押、证券借贷、配售交收等不一定是真实买卖。"]
    if "value" in res["why"] and any(not n["price"] for n in ns):
        notes.append("部分申报没有成交价，金额按最近收盘价估算。")
    if {"drift", "streak"} & set(res["why"]):
        notes.append("合计 = 自本系统上次对该申报人生成事件以来，各份明确买入 / 卖出 / 获配 / 配售申报的涉及股数 ÷ 已发行股份之和（证券借贷、质押、担保权益、「其他事项」不计）。")
    lines.append("注：" + " ".join(notes))
    x = {"source_id": f"{listing.code}:{name}:{last['serial']}", "title": title, "snippet": "\n".join(lines),
         "url": last["url"] or HK_URL.format(code=listing.code.zfill(5)), "published_at": _filed(last["serial"], last["date"]),
         "notices": ns[-8:], "why": res["why"]}
    return _item(company, listing, "data", SOURCE, x, "港交所权益披露")


def fetch_hk(app: App, company: Company, listing: Listing) -> list[str]:
    cfg = app.settings.sources
    key = f"holdchg:{listing.key}"
    st = _load(app, key)
    today = today_local()
    sid = _try(app, "hkex_di", ids.hkex_di_sid, app.db, app.session, listing)
    if sid is None:
        return [] if "hkex_di" in app.down else [f"✗ {listing.display} 找不到港交所权益披露系统里的公司编号"]
    rows = _try(app, "hkex_di", hkdi.notices, app.session, listing.code.zfill(5), sid, _start(app, st, today), today)
    if rows is None:
        return []
    done, acc = set(st.get("done", [])), dict(st.get("acc", {}))
    new = [r for r in rows if r["serial"] not in done]
    ref = _ref_price(app, listing)
    by_name: dict[str, list[dict]] = defaultdict(list)
    for n in sorted(new, key=lambda r: (r["date"], r["serial"])):
        by_name[n["name"]].append(n)
    made = 0
    for name, ns in by_name.items():
        prev = acc.get(name) if isinstance(acc.get(name), dict) else {}  # 旧版状态是一个小数，当作没有
        res = judge_hk(ns, prev, cfg.holdchg_hk_min_pct, cfg.holdchg_hk_insider_value, ref, cfg.holdchg_hk_streak)
        if res:
            made += app.db.add_item(hk_item(company, listing, name, ns, res, ref))
            acc.pop(name, None)
        elif (total := add_acc(prev, ns, ref))["n"]:
            acc[name] = total
        else:
            acc.pop(name, None)
    st.update(since=today.isoformat(), acc=acc, done=[*st.get("done", []), *(r["serial"] for r in new)])
    _save(app, key, st)
    return [f"{listing.display} 权益披露 {len(new)} 份新申报，新增持股变动 {made} 条"]


# ---------------------------------------------------------------- 美股

def _us_side(kind: str) -> tuple[str, bool] | None:
    """Nasdaq 的交易类型 → (buy|sell, 是否 10b5-1 预设计划)；期权行权、授予、非公开市场买卖返回 None。"""
    t = (kind or "").lower()
    if "non open market" in t or "option" in t:
        return None
    side = "buy" if "buy" in t else "sell" if "sell" in t else None
    return (side, "automatic" in t) if side else None


def _mdy(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%m/%d/%Y").date()
    except ValueError:
        return None


def us_insider_events(rows: list[dict], start: date, min_value: float, cluster_days: int = 30) -> list[dict]:
    """Form 4 汇总行 → 同一内部人、同一天、同方向合并后金额 ≥ min_value 的事件（事件日 ≥ start）。
    cluster = 事件日前 cluster_days 天内同方向公开市场买卖的内部人数（含金额不够的），用来标注「集中买入 / 卖出」。"""
    groups: dict[tuple, dict] = {}
    for r in rows:
        day, side = _mdy(r["date"]), _us_side(r["type"])
        if day is None or side is None or not r.get("shares"):
            continue
        g = groups.setdefault((r["name"], day, side[0], side[1]), {"name": r["name"], "relation": r["relation"], "date": day, "side": side[0],
                                                                  "auto": side[1], "shares": 0.0, "value": 0.0, "held": r.get("held")})
        g["shares"] += r["shares"]
        g["value"] += r["shares"] * (r.get("price") or 0)
    out = []
    for g in groups.values():
        if g["date"] >= start and g["value"] >= min_value:
            g["cluster"] = len({o["name"] for o in groups.values() if o["side"] == g["side"] and 0 <= (g["date"] - o["date"]).days <= cluster_days})
            out.append(g)
    return sorted(out, key=lambda g: g["date"])


def us_insider_item(company: Company, listing: Listing, g: dict) -> dict:
    side = "买入" if g["side"] == "buy" else "卖出"
    before = (g["held"] or 0) + (g["shares"] if g["side"] == "sell" else -g["shares"])
    share = f"，占其持股的 {g['shares'] / before * 100:.1f}%" if g["side"] == "sell" and before > 0 else ""
    title = f"内部人 {g['name']}（{g['relation']}）公开市场{side} {g['shares']:,.0f} 股（约 ${g['value'] / 1e6:,.2f} 百万）"
    lines = [f"{TAG} SEC Form 4（经 Nasdaq 汇总），{listing.display}。{g['date']}，{g['name']}（{g['relation']}）公开市场{side} "
             f"{g['shares']:,.0f} 股，均价约 ${g['value'] / g['shares']:,.2f}，交易后持有 {num(g['held'], 0)} 股{share}。"]
    if g["auto"]:
        lines.append("该笔为 10b5-1 预设交易计划执行（事先约定的卖出，信号弱于主动卖出）。")
    if g["cluster"] > 1:
        lines.append(f"同一窗口内共有 {g['cluster']} 位内部人公开市场{side}，属于集中{side}。")
    lines.append("注：期权行权、股权授予、税款代扣等非公开市场交易不在此列；内部人卖出的原因很多（套现、税务、分散），买入的信号通常强于卖出。")
    x = {"source_id": f"f4:{g['name']}:{g['date']}:{g['side']}:{int(g['auto'])}", "title": title, "snippet": "\n".join(lines),
         "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&ticker={listing.code}&type=4",
         "published_at": min(datetime(g["date"].year, g["date"].month, g["date"].day, 21, tzinfo=CST), utcnow()), "group": {**g, "date": str(g["date"])}}
    return _item(company, listing, "data", SOURCE, x, "Form 4")


def schedule13_event(info: dict | None, form: str, levels: dict, min_change: float) -> dict | None:
    """一份 13D / 13G 要不要报，并更新 levels（申报人 → 上次持股比例）。13D 与首次 13G 一律报；13G/A 看比例变化或降到 5% 以下。"""
    if not info:
        return {"reason": "unparsed"} if form.upper().endswith(("13D", "13D/A")) else None
    top = max(info["persons"], key=lambda p: p["percent"] or 0)
    prev = levels.get(top["name"])
    pct = top["percent"]
    if pct is not None:
        levels[top["name"]] = pct
    f = form.upper()
    if "13D" in f:
        return {"reason": "13D", "top": top, "prev": prev}
    if not f.endswith("/A"):
        return {"reason": "new", "top": top, "prev": prev}
    if pct is not None and prev is not None and (abs(pct - prev) >= min_change or (pct < 5 <= prev)):
        return {"reason": "change", "top": top, "prev": prev}
    return None


def schedule13_item(company: Company, listing: Listing, f: dict, info: dict | None, ev: dict) -> dict:
    form = f["category"]
    persons = (info or {}).get("persons") or []
    top = ev.get("top")
    if top and top["percent"] is not None:
        move = f"（上次申报 {num(ev['prev'])}%）" if ev.get("prev") is not None else ""
        title = f"{top['name']} 披露持有{listing.display} {num(top['percent'])}%{move}（{form}）"
    else:
        title = f"{listing.display} 收到 {form}"
    lines = [f"{TAG} SEC {form}，{listing.display}。"]
    if info:
        lines.append(f"第 {info['amendment'] or '—'} 号申报" + (f"，事件日 {info['event_date']}" if info["event_date"] else "") + "。申报人：")
        lines += [f"- {p['name']}：持有 {num(p['shares'], 0)} 股，占该类股份 {num(p['percent'])}%" for p in persons[:6]]
    else:
        lines.append("结构化内容解析失败，请打开原文。")
    lines.append("注：13D 表示持股超过 5% 且可能谋求影响公司控制权或战略（积极投资者、收购方），变动通常更值得看；13G 多为被动机构（基金、指数）的定期申报，"
                 "比例小幅变化常是仓位管理。")
    x = {"source_id": f["source_id"], "title": title, "snippet": "\n".join(lines), "url": f["url"], "published_at": f["published_at"],
         "persons": persons[:6], "form": form}
    return _item(company, listing, "data", SOURCE, x, form)


def fetch_us(app: App, company: Company, listing: Listing) -> list[str]:
    cfg = app.settings.sources
    key = f"holdchg:{listing.key}"
    st = _load(app, key)
    today = today_local()
    start = _start(app, st, today)
    done = set(st.get("done", []))
    msgs: list[str] = []
    new_done: list[str] = []
    made = 0
    ins = _try(app, "nasdaq", nasdaq.insiders, app.session, listing.code, 100)
    for g in us_insider_events((ins or {}).get("recent", []), start, cfg.holdchg_us_insider_value):
        uid = f"f4:{g['name']}:{g['date']}:{g['side']}:{int(g['auto'])}"
        if uid in done:
            continue
        made += app.db.add_item(us_insider_item(company, listing, g))
        new_done.append(uid)
    ua = cfg.sec_user_agent
    if ua:
        cik = _try(app, "sec", ids.sec_cik, app.db, app.session, listing, ua)
        filings = _try(app, "sec", sec.filings, app.session, cik, ua, datetime.combine(start, datetime.min.time(), CST), sec.BIG_FORMS) if cik else None
        levels = dict(st.get("levels13", {}))
        for f in sorted(filings or [], key=lambda x: x["published_at"]):
            if f["source_id"] in done:
                continue
            info = _try(app, "sec", sec.schedule13, app.session, f["url"], ua)
            if ev := schedule13_event(info, f["category"], levels, cfg.holdchg_us_filer_pct):
                made += app.db.add_item(schedule13_item(company, listing, f, info, ev))
            new_done.append(f["source_id"])
        st["levels13"] = levels
    st.update(since=today.isoformat(), done=[*st.get("done", []), *new_done])
    _save(app, key, st)
    return [f"{listing.display} 内部人 / 大股东申报新增持股变动 {made} 条"] if made or new_done else []


# ---------------------------------------------------------------- 研究 agent 的 shareholders 工具

def hk_recent_text(app: App, listing: Listing, days: int = 90) -> str:
    """近 days 天港交所权益披露的概览：按申报人汇总（最新好仓比例、买卖净额）+ 最近的买卖 / 质押申报。联网查询，失败返回空串。"""
    today = today_local()
    sid = _try(app, "hkex_di", ids.hkex_di_sid, app.db, app.session, listing)
    rows = _try(app, "hkex_di", hkdi.notices, app.session, listing.code.zfill(5), sid, today - timedelta(days=days), today) if sid else None
    if not rows:
        return ""
    by_name: dict[str, list[dict]] = defaultdict(list)
    for n in sorted(rows, key=lambda r: (r["date"], r["serial"])):
        by_name[n["name"]].append(n)
    lines = [f"{listing.display} 近 {days} 天港交所权益披露（DI；好仓比例为占已发行股份；借贷类多为被动基金的仓位管理，不是买卖）：",
             "| 申报人 | 身份 | 申报数 | 最新好仓% | 期内买入股 | 期内卖出股 | 质押/借贷申报 | 最近事件日 |", "|---|---|---|---|---|---|---|---|"]
    for name, ns in sorted(by_name.items(), key=lambda kv: kv[1][-1]["pct"].get("L") or 0, reverse=True)[:15]:
        buy = sum(n["involved"].get("L") or 0 for n in ns if hkdi.KIND_BUY in _kinds(n))
        sell = sum(n["involved"].get("L") or 0 for n in ns if hkdi.KIND_SELL in _kinds(n))
        other = sum(1 for n in ns if _kinds(n) & {hkdi.KIND_PLEDGE, hkdi.KIND_LEND})
        lines.append(f"| {name} | {ROLE[ns[-1]['kind']]} | {len(ns)} | {num(ns[-1]['pct'].get('L'))} | {buy:,.0f} | {sell:,.0f} | {other} | {ns[-1]['date']} |")
    trades = [n for n in rows if _kinds(n) & {hkdi.KIND_BUY, hkdi.KIND_SELL, hkdi.KIND_PLEDGE}]
    if trades:
        lines.append("最近的买卖 / 质押申报：")
        lines += [f"{_notice_line(n, None)}｜{n['name']}" for n in trades[:12]]
    return "\n".join(lines)
