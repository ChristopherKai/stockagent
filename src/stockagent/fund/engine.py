"""流程：盘前计划（只读库计算）、盘中提醒（只查持仓与当天条件单的快照）、盘后结算（模拟成交、净值、熔断）、
晚间日线刷新、周报。dry_run 时不写库、不推送。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

from .. import app as appmod
from ..app import FX_HKDCNH, App
from ..sources import eastmoney
from ..utils import CST, iso, log, parse_iso, today_local, utcnow
from . import calendar, data, ledger, regime, risk, store
from .config import FundsFile
from .strategy import STRATEGIES, Context, Target

SESSION = {"A": ((9, 30), (15, 0)), "HK": ((9, 30), (16, 10))}
LUNCH = {"A": ((11, 30), (13, 0)), "HK": ((12, 0), (13, 0))}


# ---------------------------------------------------------------- 每个市场共用的数据


@dataclass
class MarketData:
    market: str
    as_of: date
    expected: date
    universe: list[dict]
    dropped: dict[str, int]
    ind: dict[str, dict]
    regime: dict
    trackers: dict[str, dict]
    kill: dict[str, list[dict]]
    notes: list[str] = field(default_factory=list)


def watch_links(app: App, market: str, ind: dict[str, dict]) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """观察清单里的公司 → 情景加权目标价与证伪条件（按该市场的上市代码索引）。只读库。"""
    from .. import tracker
    from ..dossier import falsifies
    from ..monitor import current_stance

    trackers, kill = {}, {}
    for c in app.watchlist.companies:
        listings = [l for l in c.listings if l.market == market]
        if not listings:
            continue
        sids = [eastmoney.secid_for(l) for l in listings]
        trigs = [t for t in app.db.triggers(c.id) if t["status"] in ("warning", "triggered")]
        if trigs:
            stance = current_stance(app, c)
            for sid in sids:
                kill[sid] = [{**t, "falsifies": falsifies(stance, t.get("direction"))} for t in trigs]
        if c.pricing_listing.market not in ("HK", "A"):
            continue
        try:
            st = tracker.status(app, c)
        except Exception as e:  # noqa: BLE001 - 追踪数据有问题不影响基金计划
            log.warning("读取 %s 的追踪状态失败：%s", c.id, e)
            continue
        if not st or not st.get("live_target") or not st.get("price"):
            continue
        ratio = st["price"] / st["live_target"]
        for sid in sids:
            px = (ind.get(sid) or {}).get("close")
            trackers[sid] = {"company_id": c.id, "target": st["live_target"], "currency": st["currency"], "ratio": ratio,
                             "price_equiv_target": px / ratio if px else st["live_target"]}
    return trackers, kill


def market_data(app: App, cfg: FundsFile, market: str, as_of: date, after_close: bool = False, extra: list[str] = ()) -> MarketData:
    universe, dropped = data.stage1(app, cfg, market)
    secids = list(dict.fromkeys([u["secid"] for u in universe] + list(extra)))
    bars = data.load_bars(app, secids, days=420)
    ind = {sid: x for sid, b in bars.items() if (x := data.indicators(b, market))}
    expected = data.expected_bar(market, as_of, after_close)
    reg = regime.compute(app, cfg.regime, market, {u["secid"]: bars[u["secid"]] for u in universe if u["secid"] in bars})
    trackers, kill = watch_links(app, market, ind)
    md = MarketData(market, as_of, expected, universe, dropped, ind, reg, trackers, kill)
    have = sum(1 for u in universe if u["secid"] in ind)
    md.notes.append(f"股票池 {len(universe)} 只（第一阶段批量数据过滤后），其中 {have} 只有日线"
                    + ("" if have >= len(universe) * 0.8 else "；日线不足，先运行 stock fund refresh 回填"))
    if n := calendar.note(market, as_of):
        md.notes.append(n)
    if reg.get("date") and reg["date"] < expected.isoformat():
        md.notes.append(f"指数日线停在 {reg['date']}，市场状态可能过时")
    return md


def fund_defs(app: App, cfg: FundsFile) -> list[dict]:
    """funds.yaml 中、且已同步到库里的基金（库里的行带现金与熔断状态）。"""
    rows = {f["id"]: f for f in store.funds(app.db)}
    out = []
    for d in cfg.funds:
        r = rows.get(d.id)
        if r is None:
            continue
        out.append(r)
    return out


def total_cny(app: App, cfg: FundsFile, funds: list[dict], fx: float) -> tuple[float, dict[str, float]]:
    """全部基金合计资产（人民币）与按发行人合并的持仓市值（人民币）。用最近一次净值或成本估算，只读库。"""
    total, held = 0.0, {}
    for f in funds:
        rate = fx if f["currency"] == "HKD" else 1.0
        nav = store.last_nav(app.db, f["id"])
        positions = store.positions(app.db, f["id"])
        bars = data.load_bars(app, [p["secid"] for p in positions], days=10) if positions else {}
        mv = 0.0
        for p in positions:
            b = bars.get(p["secid"])
            px = float(b["close"].iloc[-1]) if b is not None and len(b) else p["avg_cost"]
            v = p["shares"] * px * rate
            mv += v
            k = risk.group_key(p)
            held[k] = held.get(k, 0.0) + v
            held.setdefault(f"fund:{f['id']}:{k}", 0.0)
            held[f"fund:{f['id']}:{k}"] += v
        total += (nav["equity"] * rate) if nav else (f["cash"] * rate + mv)
    return total, held


# ---------------------------------------------------------------- 盘前计划


@dataclass
class FundPlan:
    fund: dict
    regime: dict
    cap: float
    equity: float
    nav: float
    drawdown: float
    exposure: float
    suggestions: list[dict]
    targets: list[Target]
    notes: list[str]
    unchanged: int
    rebalance_day: bool
    positions: dict[str, dict]


def plan(app: App, cfg: FundsFile, as_of: date, fund_ids: list[str] | None = None, dry_run: bool = False) -> tuple[list[FundPlan], list[str]]:
    store.ensure(app.db)
    msgs: list[str] = []
    funds = [f for f in fund_defs(app, cfg) if not fund_ids or f["id"] in fund_ids]
    if not funds:
        return [], ["没有已同步的基金：先运行 stock fund sync"]
    fx, fx_ok = data.fx_hkdcny(app, cfg.risk.fx_hkdcny)
    if not fx_ok:
        msgs.append(f"没有港元兑人民币行情，合并计算按 {fx} 兜底")
    total, held = total_cny(app, cfg, store.funds(app.db), fx)
    out: list[FundPlan] = []
    cache: dict[str, MarketData] = {}
    for f in funds:
        m = f["market"]
        if not calendar.is_trading_day(m, as_of):
            msgs.append(f"{f['name']}：{as_of} {'港股' if m == 'HK' else 'A 股'}休市，不出计划")
            continue
        positions = {p["secid"]: p for p in store.positions(app.db, f["id"])}
        if m not in cache:
            extra = [p["secid"] for g in funds if g["market"] == m for p in store.positions(app.db, g["id"])]
            cache[m] = market_data(app, cfg, m, as_of, extra=extra)
        md = cache[m]
        out.append(_plan_fund(app, cfg, f, md, positions, total, held, fx, dry_run))
    return out, msgs


def _plan_fund(app: App, cfg: FundsFile, f: dict, md: MarketData, positions: dict[str, dict], total: float, held: dict[str, float],
               fx: float, dry_run: bool) -> FundPlan:
    strat = STRATEGIES[f["strategy"]](f["params"])
    first = not positions and not store.latest_snapshot(app.db, f["id"])
    rebalance = first or calendar.first_of_period(f["market"], md.as_of, f["rebalance"])
    ctx = Context(app=app, market=f["market"], as_of=md.as_of, expected=md.expected, universe=md.universe, ind=md.ind,
                  min_turnover=cfg.universe.min_turnover[f["market"]], participation=cfg.universe.participation,
                  rebalance_day=rebalance, trackers=md.trackers, kill=md.kill, hkdcny=fx)
    updates: dict[str, dict] = {}
    for sid, p in positions.items():
        u = strat.update(p, ctx)
        if u:
            p.update(u)
            updates[sid] = dict(u)
    prices = {sid: md.ind[sid]["close"] if sid in md.ind else p["avg_cost"] for sid, p in positions.items()}
    book = risk.Book(fund=f, cash=f["cash"], positions=positions, prices=prices)
    exits, adds = [], []
    for p in positions.values():
        sig = strat.check_exit(p, ctx)
        if sig:
            exits.append((p, sig))
            if sig.action == "review":
                updates.setdefault(p["secid"], {})["flags"] = {**(p.get("flags") or {}), "review": md.as_of.isoformat()}
            if sig.action != "review":
                continue
        t = strat.add_signal(p, ctx)
        if t:
            adds.append((p, t))
    # 趋势基金每天看突破；价值基金在调仓日重新排名（可替换持仓），平时只在有空余名额时补新标的（不替换）
    targets = strat.score(ctx) if (rebalance or f["style"] == "trend" or len(positions) < f["max_holdings"]) else []
    breaker = int(f["breaker"])
    quota = risk.new_entry_quota(f, cfg.risk, md.regime["state"], breaker, f["status"])
    lots = {}
    for code in [t.code for t in targets] + [p["code"] for p in positions.values()]:
        if f["market"] == "HK":
            lot = data.cached_lot(app, code)
            lots[code] = (int(lot), True) if lot else (100, False)
    others = {k: v for k, v in held.items() if not k.startswith("fund:")}
    for k, v in held.items():  # 扣掉本基金自己的持仓，剩下的才是「其他基金」
        if k.startswith(f"fund:{f['id']}:"):
            g = k.split(":", 2)[2]
            others[g] = others.get(g, 0.0) - v
    lim = risk.Limits(
        regime_cap=cfg.risk.exposure[f["style"]][md.regime["state"]], breaker=breaker, new_entries=quota, rebalance_day=rebalance,
        capacity={sid: c for sid, x in md.ind.items() if (c := data.liquidity_capacity(x, cfg.universe.participation))},
        other_funds=others, total_cny=total, fx_cny=fx if f["currency"] == "HKD" else 1.0, lots=lots)
    p = risk.build(book, cfg.risk, lim, exits, adds, targets, md.as_of.isoformat(), md.regime["state"])
    notes = list(md.notes) + ctx.notes + p.notes
    if breaker:
        notes.insert(0, risk.BREAKER_LABEL[breaker])
    if f["status"] != "paper":
        notes.insert(0, "基金在 funds.yaml 中设为暂停：只处理退出，不开新仓")
    unknown = [c for c, (_, ok) in lots.items() if not ok and any(s["code"] == c for s in p.suggestions)]
    if unknown:
        notes.append("每手股数未知、暂按 100 股取整（晚间任务会补查）：" + "、".join(unknown))
    last = store.last_nav(app.db, f["id"])
    fp = FundPlan(fund=f, regime=md.regime, cap=p.cap, equity=book.equity, nav=book.equity / f["initial_capital"],
                  drawdown=last["drawdown"] if last else 0.0,
                  exposure=sum(pp["shares"] * prices[s] for s, pp in positions.items()) / book.equity if book.equity else 0.0,
                  suggestions=p.suggestions, targets=targets, notes=notes, unchanged=p.unchanged, rebalance_day=rebalance,
                  positions=positions)
    if not dry_run:
        store.save_regime(app.db, {**md.regime, "date": md.regime.get("date") or md.expected.isoformat()})
        if targets:
            store.add_snapshot(app.db, f["id"], md.as_of.isoformat(), targets)
        with app.db.transaction() as c:
            store.cancel_pending(c, f["id"], md.as_of.isoformat(), "被新计划取代")
            for sid, u in updates.items():
                sets = {k: (store._dump(v) if k == "flags" else v) for k, v in u.items()}
                cols = {("flags_json" if k == "flags" else k): v for k, v in sets.items()}
                c.execute(f"UPDATE position SET {', '.join(f'{k}=?' for k in cols)}, updated_at=? WHERE fund_id=? AND secid=?",
                          (*cols.values(), store.now(), f["id"], sid))
        store.add_suggestions(app.db, p.suggestions)
    return fp


# ---------------------------------------------------------------- 盘后结算


@dataclass
class FundClose:
    fund: dict
    fills: list[ledger.Fill]
    nav: dict
    prev: dict | None
    breaker_before: int
    breaker_after: int
    positions: dict[str, dict]
    closes: dict[str, float]
    notes: list[str]


def fetch_recent(app: App, secids: list[str], persist: bool) -> tuple[dict[str, list[dict]], list[str]]:
    """收盘后取持仓、当日建议与基准的最近日线（少量，逐只）。persist=False 时不写库（--dry-run）。"""
    out, failed = {}, []
    for sid in dict.fromkeys(secids):
        bars, _ = appmod.klines(app, sid, 60)
        if not bars:
            failed.append(sid)
            continue
        out[sid] = bars
        if persist:
            app.db.upsert_bars(sid, bars)
    return out, failed


def close(app: App, cfg: FundsFile, day: date, fund_ids: list[str] | None = None, dry_run: bool = False,
          fetch: bool = True) -> tuple[list[FundClose], list[str]]:
    store.ensure(app.db)
    msgs: list[str] = []
    out = []
    funds = [f for f in fund_defs(app, cfg) if not fund_ids or f["id"] in fund_ids]
    dstr = day.isoformat()
    for f in funds:
        if not calendar.is_trading_day(f["market"], day):
            msgs.append(f"{f['name']}：{dstr} 休市，不结算")
            continue
        positions = {p["secid"]: p for p in store.positions(app.db, f["id"])}
        pending = store.suggestions(app.db, f["id"], day=dstr, statuses=("pending",))
        need = list(positions) + [s["secid"] for s in pending] + [f["benchmark"]]
        recent: dict[str, list[dict]] = {}
        if fetch:
            recent, failed = fetch_recent(app, need, persist=not dry_run)
            if failed:
                msgs.append(f"{f['name']}：{len(failed)} 只没有取到日线（{', '.join(failed[:5])}），按库里已有的计算")
        hist = data.load_bars(app, need, days=120)
        for sid, rows in recent.items():  # dry-run 时新数据只在内存里
            df = pd.DataFrame(rows)
            old = hist.get(sid)
            hist[sid] = (pd.concat([old[~old["date"].isin(df["date"])], df]).sort_values("date").reset_index(drop=True)
                         if old is not None else df)
        today_bar, prev_close, avg_vol, closes = {}, {}, {}, {}
        for sid, b in hist.items():
            ind = data.indicators(b[b["date"] <= dstr], f["market"]) if len(b) else None
            if not ind:
                continue
            closes[sid] = ind["close"]
            if ind["date"] == dstr:
                today_bar[sid] = {"date": dstr, "open": ind["open"], "high": ind["high"], "low": ind["low"], "close": ind["close"],
                                  "volume": ind["volume"]}
                prev_close[sid] = ind["prev_close"]
                avg_vol[sid] = ind["avg_vol50"]
        state = ledger.State(fund=dict(f), cash=f["cash"], positions={s: dict(p) for s, p in positions.items()})
        lots = {p["code"]: int(data.cached_lot(app, p["code"]) or 100) for p in positions.values()} if f["market"] == "HK" else {}
        for s in pending:
            if f["market"] == "HK":
                lots[s["code"]] = int((s.get("trigger") or {}).get("lot") or data.cached_lot(app, s["code"]) or 100)
        cost = cfg.cost(f["market"])
        fills = ledger.settle(state, pending, today_bar, prev_close, avg_vol, cost, dstr, lots)
        for sid, p in state.positions.items():
            if sid in closes:
                p["high_close"] = max(p.get("high_close") or 0.0, closes[sid])
        prev = store.last_nav(app.db, f["id"], before=dstr)
        first = store.navs(app.db, f["id"])
        bench_close = closes.get(f["benchmark"])
        bench_base = first[0]["benchmark_close"] if first and first[0]["benchmark_close"] else bench_close
        nav = ledger.nav_row(state, closes, dstr, prev, bench_close, bench_base)
        b0 = int(f["breaker"])
        b1 = risk.breaker_level(b0, nav["drawdown"], (f["dd1"], f["dd2"], f["dd3"]), cfg.risk.recover)
        nav["breaker"] = b1
        notes = []
        missing = [s for s in state.positions if s not in closes]
        if missing:
            notes.append(f"{len(missing)} 只持仓没有收盘价，按成本计价")
        if b1 != b0:
            notes.append(f"回撤熔断：{risk.BREAKER_LABEL[b0]} → {risk.BREAKER_LABEL[b1]}")
        out.append(FundClose(f, fills, nav, prev, b0, b1, state.positions, closes, notes))
        if not dry_run:
            _save_close(app, f, state, fills, nav, b0, b1, dstr)
    return out, msgs


def _save_close(app: App, f: dict, state: ledger.State, fills: list[ledger.Fill], nav: dict, b0: int, b1: int, day: str) -> None:
    with app.db.transaction() as c:
        for x in fills:
            s = x.suggestion
            tid = None
            if x.status == "filled":
                side = "buy" if s["action"] in ("buy", "add") else "sell"
                tid = c.execute("INSERT INTO trade (fund_id, suggestion_id, secid, code, name, side, shares, price, amount, fees, realized, "
                                "date, source, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'paper', ?, ?)",
                                (f["id"], s.get("id"), s["secid"], s.get("code"), s.get("name"), side, x.shares, x.price,
                                 x.shares * x.price, x.fees, x.realized, day, s.get("reason"), store.now())).lastrowid
            c.execute("UPDATE suggestion SET status=?, note=?, trade_id=?, updated_at=? WHERE id=?",
                      (x.status, x.note, tid, store.now(), s.get("id")))
        c.execute("UPDATE suggestion SET status='expired', note='过期未执行', updated_at=? WHERE fund_id=? AND status='pending' AND date<=?",
                  (store.now(), f["id"], day))
        store.save_positions(c, f["id"], state.positions)
        c.execute("UPDATE fund SET cash=?, breaker=?, halted_at=?, updated_at=? WHERE id=?",
                  (state.cash, b1, (iso(utcnow()) if b1 >= 3 and b0 < 3 else f["halted_at"]), store.now(), f["id"]))
        c.execute("INSERT OR REPLACE INTO nav_daily (fund_id, date, nav, equity, cash, exposure, peak, drawdown, positions, benchmark_close, "
                  "benchmark_nav, breaker, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (f["id"], day, nav["nav"], nav["equity"], nav["cash"], nav["exposure"], nav["peak"], nav["drawdown"], nav["positions"],
                   nav["benchmark_close"], nav["benchmark_nav"], b1, store.now()))


# ---------------------------------------------------------------- 盘中提醒


def in_session(market: str, now: datetime) -> bool:
    t = (now.hour, now.minute)
    (o, c), (l0, l1) = SESSION[market], LUNCH[market]
    return o <= t <= c and not (l0 < t < l1)


def elapsed_fraction(market: str, now: datetime) -> float:
    """已过去的交易时间占全天的比例（量比按全天折算用）。"""
    (o, c), (l0, l1) = SESSION[market], LUNCH[market]

    def m(hm: tuple[int, int]) -> int:
        return hm[0] * 60 + hm[1]

    t = m((now.hour, now.minute))
    total = m(c) - m(o) - (m(l1) - m(l0))
    done = min(max(t, m(o)), m(c)) - m(o) - max(0, min(t, m(l1)) - m(l0))
    return max(min(done / total, 1.0), 0.01)


@dataclass
class Alert:
    fund: dict
    secid: str
    code: str
    name: str
    kind: str                         # stop | breakout | gap | kill
    text: str
    suggestion_id: int | None = None


def quote(app: App, secid: str) -> dict | None:
    for name in app.settings.sources.price_sources:
        q = appmod._try(app, name, appmod.PRICE_SOURCES[name].fetch_quote, app.session, secid)
        if q and q.get("price"):
            return q
    return None


def alert(app: App, cfg: FundsFile, now: datetime, dry_run: bool = False, quotes: dict[str, dict] | None = None) -> tuple[list[Alert], list[str]]:
    """只看持仓与当天待触发的条件单（数量少，逐只快照）。同一只股票同一类提醒每天最多一次。"""
    store.ensure(app.db)
    now = now.astimezone(CST)
    day = now.date().isoformat()
    msgs, alerts = [], []
    quotes = dict(quotes or {})
    for f in fund_defs(app, cfg):
        m = f["market"]
        if not calendar.is_trading_day(m, now.date()) or not in_session(m, now):
            msgs.append(f"{f['name']}：不在交易时段")
            continue
        positions = {p["secid"]: p for p in store.positions(app.db, f["id"])}
        pending = [s for s in store.suggestions(app.db, f["id"], day=day, statuses=("pending",)) if s["kind"] == "conditional"]
        secids = list(dict.fromkeys(list(positions) + [s["secid"] for s in pending]))
        if not secids:
            continue
        bars = data.load_bars(app, secids, days=120)
        ind = {sid: data.indicators(b, m) for sid, b in bars.items()}
        for sid in secids:
            if sid not in quotes:
                quotes[sid] = quote(app, sid)
        frac = elapsed_fraction(m, now)
        for s in pending:
            q, trig = quotes.get(s["secid"]), s.get("trigger") or {}
            if not q:
                continue
            px = q["price"]
            if trig.get("type") == "stop" and px <= float(trig["price"]):
                alerts.append(Alert(f, s["secid"], s["code"], s["name"], "stop",
                                    f"{s['code']} {s['name']} 触及止损 {float(trig['price']):.2f}（现价 {px:.2f}），按计划清仓", s["id"]))
            elif trig.get("type") == "breakout" and px >= float(trig["level"]):
                avg = (ind.get(s["secid"]) or {}).get("avg_vol50")
                vr = q.get("volume") / frac / avg if q.get("volume") and avg else None
                need = float(trig.get("volume_ratio") or 0)
                if px > float(s.get("limit_price") or 1e18):
                    continue  # 已超过限价，不追，也不提醒
                if vr is None:
                    txt = f"{s['code']} {s['name']} 价格突破 {float(trig['level']):.2f}（现价 {px:.2f}），量能无法确认，按计划限价 ≤ {s['limit_price']:.2f} 买入前请核对"
                elif vr >= need:
                    txt = f"{s['code']} {s['name']} 放量突破 {float(trig['level']):.2f}（现价 {px:.2f}，预估量比 {vr:.1f}），按计划限价 ≤ {s['limit_price']:.2f} 买入"
                else:
                    continue
                alerts.append(Alert(f, s["secid"], s["code"], s["name"], "breakout", txt, s["id"]))
        for sid, p in positions.items():
            q = quotes.get(sid)
            if q and q.get("open") and q.get("prev_close"):
                gap = q["open"] / q["prev_close"] - 1
                if abs(gap) >= 0.05:
                    alerts.append(Alert(f, sid, p["code"], p["name"], "gap",
                                        f"{p['code']} {p['name']} 跳空{'高开' if gap > 0 else '低开'} {gap:+.1%}（开盘 {q['open']:.2f}），按盘前计划处理，不追加操作"))
        for c in app.watchlist.companies:
            for l in c.listings:
                sid = eastmoney.secid_for(l) if l.market == m else None
                if sid in positions:
                    for t in app.db.triggers(c.id):
                        if t["status"] == "triggered" and t.get("triggered_at") and parse_iso(t["triggered_at"]).astimezone(CST).date() == now.date():
                            alerts.append(Alert(f, sid, positions[sid]["code"], positions[sid]["name"], "kill",
                                                f"{positions[sid]['code']} {positions[sid]['name']} 证伪条件已触发：{t['description']}（明日盘前计划会给出处理）"))
    fresh = []
    for a in alerts:
        key = f"fund_alert:{day}:{a.fund['id']}:{a.secid}:{a.kind}"
        if app.db.kv_get(key):
            continue
        fresh.append(a)
        if not dry_run:
            app.db.kv_set(key, iso(utcnow()))
            if a.suggestion_id:
                store.mark_alerted(app.db, a.suggestion_id)
    return fresh, msgs


# ---------------------------------------------------------------- 晚间刷新


def refresh(app: App, cfg: FundsFile, markets: list[str] | None = None, limit: int | None = None, dry_run: bool = False,
            progress=print) -> list[str]:
    """收盘后：指数、汇率、每个市场第一阶段股票池 + 持仓的日线；港股候选与持仓的每手股数。"""
    store.ensure(app.db)
    msgs = []
    funds = fund_defs(app, cfg)
    for m in markets or sorted({f["market"] for f in funds}):
        universe, dropped = data.stage1(app, cfg, m)
        held = [p["secid"] for f in funds if f["market"] == m for p in store.positions(app.db, f["id"])]
        secids = list(dict.fromkeys([u["secid"] for u in universe] + held))
        label = "港股" if m == "HK" else "A 股"
        msgs.append(f"{label}：第一阶段股票池 {len(universe)} 只（"
                    + "，".join(f"{k} {v}" for k, v in sorted(dropped.items(), key=lambda x: -x[1])) + f"），持仓 {len(held)} 只")
        idx = [cfg.regime.index[m]] + [f["benchmark"] for f in funds if f["market"] == m] + ([FX_HKDCNH] if m == "HK" else [])
        if dry_run:
            have = {r["secid"] for r in app.db._all(
                f"SELECT DISTINCT secid FROM bars WHERE secid IN ({', '.join('?' * len(secids))})", secids)} if secids else set()
            n_new = len([s for s in secids if s not in have])
            msgs.append(f"  dry-run：需要回填 {n_new} 只、增量更新 {len(secids) - n_new} 只（每只约 1 次请求，间隔 "
                        f"{cfg.bars.request_delay}s，估计 {len(secids) * max(cfg.bars.request_delay, 0.3) / 60:.0f} 分钟）")
            continue
        for sid in dict.fromkeys(idx):
            bars, _ = appmod.klines(app, sid, cfg.regime.vol_lookback + cfg.regime.ma + 60)
            if bars:
                app.db.upsert_bars(sid, bars)
            else:
                msgs.append(f"  ✗ 没有取到 {sid} 的日线")
        st = data.refresh_bars(app, cfg, secids, limit=limit, progress=progress)
        rate = f"，{st.rate:.2f} 次/秒" if st.rate else ""
        msgs.append(f"  日线：请求 {st.requested} 次（成功 {st.ok}，其中全量 {st.full}；失败 {st.failed}），用时 {st.seconds:.0f} 秒{rate}"
                    + (f"；{st.aborted}" if st.aborted else ""))
        if m == "HK":
            codes = [p["code"] for f in funds if f["market"] == "HK" for p in store.positions(app.db, f["id"])]
            for f in funds:
                if f["market"] != "HK":
                    continue
                md = market_data(app, cfg, "HK", today_local(), after_close=True)
                ctx = Context(app=app, market="HK", as_of=md.as_of, expected=md.expected, universe=md.universe, ind=md.ind,
                              min_turnover=cfg.universe.min_turnover["HK"], participation=cfg.universe.participation,
                              trackers=md.trackers, kill=md.kill, hkdcny=data.fx_hkdcny(app, cfg.risk.fx_hkdcny)[0])
                codes += [t.code for t in STRATEGIES[f["strategy"]](f["params"]).score(ctx)]
            n = data.refresh_lots(app, cfg, list(dict.fromkeys(codes)))
            msgs.append(f"  港股每手股数：查询 {len(set(codes))} 只，新取得 {n} 只")
    return msgs
