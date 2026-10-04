"""推送与命令行输出的文字（全部由代码拼出，数字来自计算结果）。推送单条超过上限时按基金拆分。"""

from __future__ import annotations

from datetime import date, timedelta

from ..app import App
from ..utils import fmt_local
from . import ledger, regime, store
from .risk import ACTION_LABEL, BREAKER_LABEL

DISCLAIMER = "模拟组合信号，非投资建议（模拟基金不对接券商、不下单）"
TITLE = "【模拟基金】"
MAX_CHARS = 3800                     # Telegram 单条 4000 字符，留出标题与免责声明
MARKET_LABEL = {"HK": "港股", "A": "A 股"}


def _w(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def _px(x: float | None) -> str:
    return "—" if x is None else f"{x:,.3f}".rstrip("0").rstrip(".") if x < 10 else f"{x:,.2f}"


def _shares(n) -> str:
    return f"{int(n):,} 股"


def suggestion_lines(s: dict, equity: float) -> list[str]:
    t = s.get("trigger") or {}
    head = f"· {s['code']} {s['name']} "
    if s["action"] == "review":
        return [head + "复核论点", f"  理由：{s['reason']}"]
    if s["action"] in ("sell", "trim"):
        verb = "清仓" if s["action"] == "sell" else "减仓"
        return [head + f"{verb} {_w(s['cur_weight'])} → {_w(s['target_weight'])}（{_shares(s['shares'])}，开盘卖）",
                f"  理由：{s['reason']}"]
    lines = []
    if s["kind"] == "conditional" and t.get("type") == "breakout":
        cond = f"条件：突破 {_px(t['level'])} 且量比 ≥ {t.get('volume_ratio')}，限价 ≤ {_px(s['limit_price'])}"
        lines.append(head + f"{_w(s['cur_weight'])} → {_w(s['target_weight'])}（{t.get('tranche', '')}；{_shares(s['shares'])}）")
        lines.append("  " + cond)
    else:
        verb = "加仓" if s["action"] == "add" else ""
        lines.append(head + f"{verb}{_w(s['cur_weight'])} → {_w(s['target_weight'])}（"
                     + (f"{t['tranche']}；" if t.get("tranche") else "") + f"{_shares(s['shares'])}，开盘买，限价 ≤ {_px(s['limit_price'])}）")
    if s.get("stop"):
        level = t.get("level") or s.get("limit_price")
        risk_pct = s["shares"] * (level - s["stop"]) / equity if level and equity else None
        lines.append(f"  止损：{_px(s['stop'])}" + (f"（{s['stop'] / level - 1:+.1%}，风险占净值 {_w(risk_pct)}）" if level else ""))
    lines.append(f"  理由：{s['reason']}")
    if t.get("flag"):
        lines.append(f"  ⚑ {t['flag']}")
    if not t.get("lot_known", True):
        lines.append("  注意：每手股数未知，暂按 100 股取整，下单前核对")
    lines.append("  失效：今日未" + ("触发" if s["kind"] == "conditional" else "成交") + "则作废")
    return lines


def fund_plan_block(fp) -> str:
    f = fp.fund
    out = [f"{MARKET_LABEL[f['market']]} · {f['name']}" + ("（调仓日）" if fp.rebalance_day else ""),
           f"市场状态：{regime.describe(fp.regime, fp.cap)} 净值 {fp.nav:.4f} 回撤 {fp.drawdown:.1%} 仓位 {_w(fp.exposure)}"]
    sells = [s for s in fp.suggestions if s["action"] in ("sell", "trim")]
    buys = [s for s in fp.suggestions if s["action"] in ("buy", "add")]
    reviews = [s for s in fp.suggestions if s["action"] == "review"]
    stops = [s for s in fp.suggestions if s["action"] == "stop"]
    if sells:
        out.append(f"\n卖出 {len(sells)} 笔")
        for s in sells:
            out += suggestion_lines(s, fp.equity)
    if buys:
        out.append(f"\n买入 {len(buys)} 笔")
        for s in buys:
            out += suggestion_lines(s, fp.equity)
    if reviews:
        out.append(f"\n复核 {len(reviews)} 只")
        for s in reviews:
            out += suggestion_lines(s, fp.equity)
    if stops:
        out.append("\n止损条件单：" + "；".join(f"{s['code']} {s['name']} {_px(s['stop'])}" for s in stops))
    if not (sells or buys or reviews):
        out.append("\n今日无操作")
    if fp.unchanged:
        out.append(f"不动：其余 {fp.unchanged} 只，权重偏离均在 5 个百分点内")
    notes = [n for n in fp.notes if n]
    if notes:
        out.append("提示：" + "；".join(notes[:8]))
    return "\n".join(out)


def chunks(title: str, blocks: list[str]) -> list[tuple[str, str]]:
    """合并成推送消息；超过 MAX_CHARS 时按基金拆成多条。"""
    text = "\n\n".join(blocks) + f"\n\n{DISCLAIMER}"
    if len(text) <= MAX_CHARS:
        return [(title, text)]
    out = []
    for i, b in enumerate(blocks, 1):
        body = b if len(b) <= MAX_CHARS else b[:MAX_CHARS - 20] + "\n……（已截断，完整内容见 stock fund status）"
        out.append((f"{title}（{i}/{len(blocks)}）", body + f"\n\n{DISCLAIMER}"))
    return out


def plan_messages(plans: list, day: date, extra: list[str] = ()) -> list[tuple[str, str]]:
    blocks = [fund_plan_block(fp) for fp in sorted(plans, key=lambda p: (p.fund["market"] != "HK", p.fund["id"]))]
    if extra:
        blocks.append("\n".join(extra))
    return chunks(f"{TITLE}盘前计划 {day:%m-%d}", blocks or ["今日无计划"])


def alert_messages(alerts: list, now) -> list[tuple[str, str]]:
    lines = [f"· [{a.fund['name']}] {a.text}" for a in alerts]
    return chunks(f"{TITLE}盘中提醒 {now:%m-%d %H:%M}", ["\n".join(lines)])


def close_block(app: App, fc) -> str:
    f, nav, prev = fc.fund, fc.nav, fc.prev
    day_ret = nav["nav"] / prev["nav"] - 1 if prev else nav["nav"] - 1
    bench = ""
    if nav.get("benchmark_nav") and prev and prev.get("benchmark_nav"):
        bench = f"，基准 {nav['benchmark_nav'] / prev['benchmark_nav'] - 1:+.2%}"
    out = [f"{MARKET_LABEL[f['market']]} · {f['name']}",
           (f"净值 {nav['nav']:.4f}（当日 {day_ret:+.2%}{bench}） 回撤 {nav['drawdown']:.1%} 仓位 {_w(nav['exposure'])} "
            f"现金 {nav['cash']:,.0f} {f['currency']} 持仓 {nav['positions']} 只")]
    done = [x for x in fc.fills if x.status == "filled"]
    miss = [x for x in fc.fills if x.status == "unfilled"]
    if done:
        out.append("模拟成交：" + "；".join(
            f"{'买' if x.suggestion['action'] in ('buy', 'add') else '卖'} {x.suggestion['code']} {x.suggestion['name']} "
            f"{_shares(x.shares)} @ {_px(x.price)}" + (f"（盈亏 {x.realized:+,.0f}）" if x.realized is not None else "") for x in done))
    if miss:
        out.append("未能成交：" + "；".join(f"{x.suggestion['code']} {x.suggestion['name']}（{x.note}）" for x in miss))
    if not done and not miss:
        out.append("今日没有成交")
    movers = sorted(((p, fc.closes.get(s)) for s, p in fc.positions.items() if fc.closes.get(s)),
                    key=lambda x: -abs(x[1] / x[0]["avg_cost"] - 1))[:5]
    if movers:
        out.append("持仓浮盈亏：" + "；".join(f"{p['code']} {p['name']} {c / p['avg_cost'] - 1:+.1%}" for p, c in movers))
    if fc.breaker_after != fc.breaker_before or fc.breaker_after:
        out.append(f"熔断：{BREAKER_LABEL[fc.breaker_after]}" + ("（运行 stock fund resume 手动恢复）" if fc.breaker_after >= 3 else ""))
    watch = [s for s in store.latest_snapshot(app.db, f["id"]) if s["secid"] not in fc.positions][:5]
    if watch:
        out.append("明日观察：" + "、".join(f"{s['code']} {s['name']}" for s in watch))
    out += [f"提示：{n}" for n in fc.notes]
    return "\n".join(out)


def close_messages(app: App, results: list, day: date, extra: list[str] = ()) -> list[tuple[str, str]]:
    blocks = [close_block(app, fc) for fc in sorted(results, key=lambda r: (r.fund["market"] != "HK", r.fund["id"]))]
    if extra:
        blocks.append("\n".join(extra))
    return chunks(f"{TITLE}盘后复盘 {day:%m-%d}", blocks or ["今日没有需要结算的基金"])


def weekly_block(app: App, f: dict, day: date) -> str:
    navs = store.navs(app.db, f["id"])
    if not navs:
        return f"{MARKET_LABEL[f['market']]} · {f['name']}\n还没有净值记录"
    last = navs[-1]
    week_start = (day - timedelta(days=day.weekday())).isoformat()
    before = [n for n in navs if n["date"] < week_start]
    base = before[-1] if before else {"nav": 1.0, "benchmark_nav": 1.0}
    wk = last["nav"] / base["nav"] - 1
    bwk = (last["benchmark_nav"] / base["benchmark_nav"] - 1) if last.get("benchmark_nav") and base.get("benchmark_nav") else None
    total = last["nav"] - 1
    btot = (last["benchmark_nav"] - 1) if last.get("benchmark_nav") else None
    trades = store.trades(app.db, f["id"])
    week_trades = [t for t in trades if t["date"] >= week_start]
    st = ledger.trade_stats(trades)
    mdd = ledger.max_drawdown([n["nav"] for n in navs])
    out = [f"{MARKET_LABEL[f['market']]} · {f['name']}（自 {f['inception']} 起）",
           f"本周 {wk:+.2%}" + (f"（基准 {bwk:+.2%}，超额 {wk - bwk:+.2%}）" if bwk is not None else "")
           + f"；累计 {total:+.2%}" + (f"（基准 {btot:+.2%}）" if btot is not None else ""),
           f"净值 {last['nav']:.4f} 最大回撤 {mdd:.1%} 当前回撤 {last['drawdown']:.1%} 仓位 {_w(last['exposure'])} 持仓 {last['positions']} 只",
           f"本周成交 {len(week_trades)} 笔；已平仓 {st['n']} 笔"
           + (f"，胜率 {st['win_rate']:.0%}，盈亏比 {st['payoff']:.2f}" if st["win_rate"] is not None and st["payoff"] else "")
           + (f"，每笔期望 {st['expectancy']:+,.0f} {f['currency']}" if st["expectancy"] is not None else "")]
    if f["breaker"]:
        out.append(f"熔断：{BREAKER_LABEL[f['breaker']]}")
    return "\n".join(out)


def weekly_messages(app: App, funds: list[dict], day: date) -> list[tuple[str, str]]:
    blocks = [weekly_block(app, f, day) for f in sorted(funds, key=lambda f: (f["market"] != "HK", f["id"]))]
    blocks.append("样本少时胜率、盈亏比不可靠；第一期只有模拟盘，没有回测对照。")
    return chunks(f"{TITLE}周报 {day:%m-%d}", blocks)


def status_text(app: App, funds: list[dict]) -> str:
    out = []
    for f in funds:
        nav = store.last_nav(app.db, f["id"])
        out.append(f"{f['id']} · {f['name']}（{MARKET_LABEL[f['market']]}，{f['strategy']}，{f['status']}"
                   + (f"，{BREAKER_LABEL[f['breaker']]}" if f["breaker"] else "") + "）")
        if nav:
            out.append(f"  净值 {nav['nav']:.4f}（{nav['date']}）回撤 {nav['drawdown']:.1%} 仓位 {_w(nav['exposure'])} 现金 {nav['cash']:,.0f} {f['currency']}")
        else:
            out.append(f"  尚未结算：现金 {f['cash']:,.0f} {f['currency']}（自 {f['inception']} 起）")
        for p in store.positions(app.db, f["id"]):
            out.append(f"  持仓 {p['code']} {p['name']} {_shares(p['shares'])} 成本 {_px(p['avg_cost'])}"
                       + (f" 止损 {_px(p['stop'])}" if p.get("stop") else "") + f" 第 {p['tranches']} 批 建仓 {p['opened_on']}")
        pend = store.suggestions(app.db, f["id"], statuses=("pending",))
        for s in pend:
            out.append(f"  待执行 {s['date']} {ACTION_LABEL.get(s['action'], s['action'])} {s['code']} {s['name']} {_shares(s['shares'])}"
                       f"（{'条件单' if s['kind'] == 'conditional' else '开盘'}）")
        snap = store.latest_snapshot(app.db, f["id"])
        if snap:
            out.append(f"  最近打分 {snap[0]['date']}：" + "、".join(f"{s['code']} {s['name']}({s['score']:.0f})" for s in snap[:8]))
        r = store.regime(app.db, f["market"])
        if r:
            out.append(f"  市场状态 {regime.LABEL[r['state']]}（{r['date']}，记录于 {fmt_local(r['created_at'])}）")
    return "\n".join(out) or "还没有基金：先运行 stock fund sync"
