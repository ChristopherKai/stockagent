"""模拟基金页面：总览、基金详情、操作建议（已执行 / 忽略回执）、实际持仓、恢复熔断。

页面只读数据库，不联网（价格取库里最近的日线）。回执记在 user_action，不改动模拟账本：
模拟持仓、净值照旧按规则在盘后结算；「实际持仓」由回执推算，与模拟持仓对照。所有页面都标注「模拟组合信号，非投资建议」。
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from ..fund import config as fcfg
from ..fund import data, guards, ledger, regime, risk, store
from ..fund.report import DISCLAIMER as FUND_DISCLAIMER
from ..fund.report import MARKET_LABEL as FUND_MARKET
from ..screen import store as sc_store
from . import charts
from .security import verify_csrf
from .server import page
from .views import back, holder

router = APIRouter(dependencies=[Depends(verify_csrf)])

STATUS_LABEL = {"pending": "待执行", "filled": "已模拟成交", "expired": "已失效", "unfilled": "未能成交", "cancelled": "已取消",
                "note": "提醒"}
KIND_LABEL = {"open": "开盘", "conditional": "条件单", "note": "提醒"}
USER_LABEL = {"executed": "已执行", "ignored": "已忽略"}
TRADE_ACTIONS = ("buy", "add", "trim", "sell", "stop")


def _cfg(app) -> tuple[fcfg.FundsFile | None, str | None]:
    try:
        return fcfg.load(app), None
    except fcfg.FundsError as e:
        return None, str(e)


def _closes(app, secids: list[str]) -> dict[str, tuple[float, str]]:
    """库里最近的收盘价（不联网）。"""
    bars = data.load_bars(app, list(dict.fromkeys(secids)), days=15) if secids else {}
    return {sid: (float(b["close"].iloc[-1]), str(b["date"].iloc[-1])) for sid, b in bars.items() if len(b)}


def _holdings(app, f: dict) -> tuple[list[dict], float, float]:
    """模拟持仓（按库里最近收盘价计市值）→ (行, 持仓市值, 总资产)。"""
    positions = store.positions(app.db, f["id"])
    closes = _closes(app, [p["secid"] for p in positions])
    rows, mv = [], 0.0
    for p in positions:
        close, day = closes.get(p["secid"], (None, None))
        px = close if close is not None else p["avg_cost"]
        v = p["shares"] * px
        mv += v
        rows.append({**p, "close": close, "close_date": day, "value": v, "pnl": (px / p["avg_cost"] - 1) if p["avg_cost"] else None,
                     "pnl_amt": p["shares"] * (px - p["avg_cost"])})
    equity = f["cash"] + mv
    for r in rows:
        r["weight"] = r["value"] / equity if equity > 0 else None
    return rows, mv, equity


def _summary(app, f: dict, cfg: fcfg.FundsFile | None) -> dict:
    navs = store.navs(app.db, f["id"])
    rows, mv, equity = _holdings(app, f)
    reg = store.regime(app.db, f["market"])
    cap = risk.exposure_cap(f, cfg.risk, reg["state"], int(f["breaker"])) if cfg and reg else None
    last = navs[-1] if navs else None
    nav = equity / f["initial_capital"]
    bench = last.get("benchmark_nav") if last else None
    plan = app.db._one("SELECT MAX(date) AS d FROM suggestion WHERE fund_id=?", (f["id"],))
    return {
        "fund": f, "navs": navs, "last": last, "nav": nav, "ret": nav - 1, "bench_ret": (bench - 1) if bench else None,
        "excess": (nav - bench) if bench else None, "drawdown": last["drawdown"] if last else 0.0,
        "max_dd": ledger.max_drawdown([n["nav"] for n in navs]) if navs else 0.0, "exposure": mv / equity if equity > 0 else 0.0,
        "equity": equity, "cash": f["cash"], "holdings": rows, "regime": reg, "cap": cap, "last_plan": plan["d"] if plan else None,
        "chart": charts.nav_svg(navs[-250:]), "stats": ledger.trade_stats(store.trades(app.db, f["id"])),
    }


def _fund_or_404(app, fund_id: str) -> dict:
    f = store.fund(app.db, fund_id)
    if f is None:
        raise HTTPException(404, "没有这只基金")
    return f


def _ctx(**kw) -> dict:
    return {"FUND_DISCLAIMER": FUND_DISCLAIMER, "FUND_MARKET": FUND_MARKET, "ACTION_LABEL": risk.ACTION_LABEL,
            "BREAKER_LABEL": risk.BREAKER_LABEL, "REGIME_LABEL": regime.LABEL, "STATUS_LABEL": STATUS_LABEL,
            "KIND_LABEL": KIND_LABEL, "USER_LABEL": USER_LABEL, **kw}


@router.get("/funds")
def funds_page(request: Request):
    app = holder(request).app
    cfg, err = _cfg(app)
    funds = store.funds(app.db)
    summaries = [_summary(app, f, cfg) for f in funds]
    fx, fx_ok = data.fx_hkdcny(app, cfg.risk.fx_hkdcny if cfg else 0.92)
    total = sum(s["equity"] * (fx if s["fund"]["currency"] == "HKD" else 1.0) for s in summaries)
    initial = sum(s["fund"]["initial_capital"] * (fx if s["fund"]["currency"] == "HKD" else 1.0) for s in summaries)
    configured = {d.id for d in cfg.funds} if cfg else set()
    return page(request, "funds.html", **_ctx(summaries=summaries, total=total, total_ret=(total / initial - 1) if initial else None,
                                               fx=fx, fx_ok=fx_ok, cfg_err=err, configured=configured))


def _suggestion_rows(app, rows: list[dict]) -> list[dict]:
    acts = {a["suggestion_id"]: a for a in store.actions(app.db)}
    trades = {t["suggestion_id"]: t for t in store.trades(app.db) if t.get("suggestion_id")}
    closes = _closes(app, [s["secid"] for s in rows])
    out = []
    for s in rows:
        t = s.get("trigger") or {}
        ref = (trades[s["id"]]["price"] if s["id"] in trades else s.get("limit_price") or t.get("level") or t.get("price")
               or (closes.get(s["secid"]) or (None,))[0])
        out.append({**s, "user": acts.get(s["id"]), "model_trade": trades.get(s["id"]), "ref_price": ref})
    return out


@router.get("/funds/suggestions")
def suggestions_page(request: Request, days: int = 7, fund: str | None = None):
    app = holder(request).app
    store.ensure(app.db)
    days = max(1, min(days, 90))
    first = app.db._one("SELECT MAX(date) AS d FROM suggestion")
    rows = []
    if first and first["d"]:
        since = (date.fromisoformat(first["d"]) - timedelta(days=days)).isoformat()
        rows = [s for s in store.suggestions(app.db, fund) if s["date"] >= since]
    rows.sort(key=lambda s: (s["date"], s.get("priority") or 9), reverse=False)
    rows = _suggestion_rows(app, rows)
    by_day: dict[str, list[dict]] = {}
    for s in sorted(rows, key=lambda s: s["date"], reverse=True):
        by_day.setdefault(s["date"], []).append(s)
    names = {f["id"]: f["name"] for f in store.funds(app.db)}
    return page(request, "fund_suggestions.html", **_ctx(by_day=by_day, days=days, fund=fund, names=names, TRADE_ACTIONS=TRADE_ACTIONS))


@router.get("/funds/actual")
def actual_page(request: Request):
    app = holder(request).app
    out = []
    for f in store.funds(app.db):
        model = {p["secid"]: p for p in store.positions(app.db, f["id"])}
        actual = store.actual_holdings(app.db, f["id"])
        closes = _closes(app, list(model) + list(actual))
        rows = []
        for sid in sorted(set(model) | set(actual)):
            m, a = model.get(sid), actual.get(sid)
            close = (closes.get(sid) or (None,))[0]
            rows.append({"secid": sid, "code": (m or a)["code"], "name": (m or a)["name"], "model": m, "actual": a, "close": close,
                         "diff": (a["shares"] if a else 0) - (m["shares"] if m else 0),
                         "actual_pnl": (a["shares"] * (close - a["avg_cost"]) + a["realized"]) if a and close is not None else None})
        out.append({"fund": f, "rows": rows, "n_actions": len(store.actions(app.db, f["id"]))})
    return page(request, "fund_actual.html", **_ctx(funds=out))


@router.get("/funds/{fund_id}")
def fund_page(request: Request, fund_id: str):
    app = holder(request).app
    f = _fund_or_404(app, fund_id)
    cfg, err = _cfg(app)
    s = _summary(app, f, cfg)
    pending = _suggestion_rows(app, [x for x in store.suggestions(app.db, f["id"], day=s["last_plan"])] if s["last_plan"] else [])
    snap = store.latest_snapshot(app.db, f["id"])
    for r in snap:  # 最近一次打分的候选：附上模型否决检查的缓存结果
        u = sc_store.stock(app.db, r["secid"]) or {}
        r["veto"] = guards.cached(app, r["secid"], (u.get("info") or {}).get("report_date"))
    excluded = []
    if cfg:
        universe, _ = data.stage1(app, cfg, f["market"])
        ex, _ = guards.exclusions(app, cfg, universe)
        names = {u["secid"]: u for u in universe}
        excluded = [{"code": names[k]["code"], "name": names[k]["name"], "reason": v} for k, v in ex.items() if k in names]
    regimes = [store._decode(r) for r in app.db._all("SELECT * FROM market_regime WHERE market=? ORDER BY date DESC LIMIT 20",
                                                     (f["market"],))]
    fdef = cfg.fund(f["id"]) if cfg else None
    return page(request, "fund.html", **_ctx(
        s=s, f=f, pending=pending, snap=snap, excluded=excluded, regimes=regimes, cfg=cfg, cfg_err=err, fdef=fdef,
        trades=list(reversed(store.trades(app.db, f["id"])))[:40], navs=list(reversed(s["navs"]))[:30],
        params_json=json.dumps(f["params"], ensure_ascii=False, indent=2), TRADE_ACTIONS=TRADE_ACTIONS))


# ---------------------------------------------------------------- 回执与恢复（写操作：CSRF 由 router 依赖校验）


def _num(v, name: str, integer: bool = False) -> float | None:
    v = (v or "").strip().replace(",", "")
    if not v:
        return None
    try:
        x = float(v)
    except ValueError as e:
        raise ValueError(f"{name}不是数字：{v}") from e
    if not math.isfinite(x) or x <= 0:
        raise ValueError(f"{name}须为正数")
    if integer and abs(x - round(x)) > 1e-9:
        raise ValueError(f"{name}须为整数")
    return x


def _return_to(request: Request, default: str) -> str:
    ref = request.headers.get("referer") or ""
    path = ref.split("://", 1)[-1].split("/", 1)[-1] if "://" in ref else ""
    return "/" + path if path.startswith("funds") else default


@router.post("/funds/suggestions/{suggestion_id}/{action}")
async def suggestion_action(request: Request, suggestion_id: int, action: str):
    app = holder(request).app
    s = store.suggestion(app.db, suggestion_id)
    if s is None:
        raise HTTPException(404, "没有这条建议")
    to = _return_to(request, "/funds/suggestions")
    if action == "clear":
        store.clear_action(app.db, suggestion_id)
        return back(to, ok=f"已撤销对 {s['code']} {s['name']} 的回执")
    if action not in ("execute", "ignore"):
        raise HTTPException(404)
    if s["action"] not in TRADE_ACTIONS:
        return back(to, err="提醒类建议不需要回执")
    form = await request.form()
    note = (form.get("note") or "").strip()[:200] or None
    if action == "ignore":
        store.record_action(app.db, s, "ignored", note=note)
        return back(to, ok=f"已记录：忽略 {s['code']} {s['name']}（模拟账本不受影响）")
    try:
        shares = _num(form.get("shares"), "成交股数", integer=True)
        price = _num(form.get("price"), "成交价")
    except ValueError as e:
        return back(to, err=str(e))
    estimated = shares is None or price is None
    if shares is None:
        shares = float(s["shares"] or 0)
    if price is None:
        price = next(r["ref_price"] for r in _suggestion_rows(app, [s]))
    if not shares or not price:
        return back(to, err="缺少成交股数或成交价，请填写")
    store.record_action(app.db, s, "executed", shares=shares, price=price, estimated=estimated, note=note)
    return back(to, ok=f"已记录：{s['code']} {s['name']} {int(shares):,} 股 @ {price:g}"
                + ("（未填写的按建议数量 / 参考价）" if estimated else "") + "；模拟账本不受影响")


@router.post("/funds/{fund_id}/resume")
async def fund_resume(request: Request, fund_id: str):
    app = holder(request).app
    f = _fund_or_404(app, fund_id)
    form = await request.form()
    if form.get("confirm") != "yes":
        return back(f"/funds/{fund_id}", err="请先勾选「我已复核」再恢复")
    if not f["breaker"]:
        return back(f"/funds/{fund_id}", err="这只基金没有熔断")
    store.resume(app.db, fund_id)
    return back(f"/funds/{fund_id}", ok=f"{f['name']} 已恢复：熔断清零，下一次盘前计划起恢复开新仓（回撤仍按历史最高净值计算）")
