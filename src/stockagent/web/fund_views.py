"""模拟基金页面：总览、基金详情、操作建议（已执行 / 忽略回执）、实际持仓、恢复熔断。

页面只读数据库，不联网（价格取库里最近的日线）。回执记在 user_action，不改动模拟账本：
模拟持仓、净值照旧按规则在盘后结算；「实际持仓」由回执推算，与模拟持仓对照。所有页面都标注「模拟组合信号，非投资建议」。
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request

from ..fund import config as fcfg
from ..fund import data, guards, ledger, regime, risk, store
from ..fund.report import DISCLAIMER as FUND_DISCLAIMER
from ..fund.report import MARKET_LABEL as FUND_MARKET
from ..fund.strategy import STRATEGIES
from ..screen import store as sc_store
from ..utils import today_local
from . import charts, yamledit
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
    nav = equity / store.units(app.db, f)
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
    initial = sum((s["fund"]["initial_capital"] + sum(x["amount"] for x in store.flows(app.db, s["fund"]["id"])))
                  * (fx if s["fund"]["currency"] == "HKD" else 1.0) for s in summaries)
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


BENCHMARKS = {"HK": "100.HSI", "A": "1.000300"}
DEFAULT_FORM = {"market": "HK", "strategy": "hk_quality_value", "max_holdings": 10, "initial_capital": 1_000_000, "max_weight": 0.15,
                "sector_cap": 0.30, "min_weight": 0.03, "drawdown": [0.05, 0.10, 0.15], "rebalance": "monthly", "benchmark": "100.HSI",
                "status": "paper", "params": {}}


def _strategies() -> list[dict]:
    out = []
    for k, cls in STRATEGIES.items():
        defaults = {n: f.default for n, f in cls.Params.model_fields.items()}
        out.append({"key": k, "label": cls.label, "market": cls.market, "style": cls.style, "defaults": yaml.safe_dump(
            defaults, allow_unicode=True, sort_keys=False, default_flow_style=False).strip(), "doc": (cls.__doc__ or "").strip()})
    return out


def _form_page(request: Request, values: dict, fund: dict | None, err: str | None = None, status_code: int = 200):
    h = holder(request)
    path = fcfg.path(h.app)
    etag = yamledit.digest(path.read_text(encoding="utf-8")) if path.exists() else ""
    locked = bool(fund) and store.has_history(h.app.db, fund["id"])
    held = len(store.positions(h.app.db, fund["id"])) if fund else 0
    params_text = yaml.safe_dump(values.get("params") or {}, allow_unicode=True, sort_keys=False, default_flow_style=False).strip()
    return page(request, "fund_form.html", status_code=status_code, **_ctx(
        v=values, fund=fund, err=err, etag=etag, locked=locked, held=held, strategies=_strategies(), params_text=params_text
        if values.get("params") else "", BENCHMARKS=BENCHMARKS))


@router.get("/funds/new")
def fund_new(request: Request):
    return _form_page(request, dict(DEFAULT_FORM), None)


@router.get("/funds/{fund_id}/edit")
def fund_edit(request: Request, fund_id: str):
    app = holder(request).app
    f = _fund_or_404(app, fund_id)
    cfg, err = _cfg(app)
    d = cfg.fund(fund_id) if cfg else None
    if d is None:
        return back(f"/funds/{fund_id}", err=err or "funds.yaml 里没有这只基金（可能已被移除），无法编辑")
    return _form_page(request, d.model_dump(), f)


def _pct_field(form, name: str) -> float:
    v = (form.get(name) or "").strip()
    try:
        return float(v) / 100
    except ValueError as e:
        raise ValueError(f"{name} 不是数字：{v}") from e


def _parse_fund_form(form) -> dict:
    strategy = (form.get("strategy") or "").strip()
    cls = STRATEGIES.get(strategy)
    if cls is None:
        raise ValueError(f"未知策略：{strategy}")
    try:
        params = yaml.safe_load(form.get("params") or "") or {}
    except yaml.YAMLError as e:
        raise ValueError(f"策略参数不是有效的 YAML：{e}") from e
    params = params if isinstance(params, dict) else None
    if params is None:
        raise ValueError("策略参数须是「名称: 值」的列表")
    try:
        capital, holdings = float(form.get("initial_capital") or 0), int(form.get("max_holdings") or 0)
    except ValueError as e:
        raise ValueError("初始资金与标的上限须是数字") from e
    return {
        "id": (form.get("id") or "").strip(), "name": (form.get("name") or "").strip(), "market": cls.market, "strategy": strategy,
        "style": cls.style, "currency": fcfg.MARKET_CCY[cls.market], "initial_capital": capital, "max_holdings": holdings,
        "max_weight": _pct_field(form, "max_weight"), "sector_cap": _pct_field(form, "sector_cap"), "min_weight": _pct_field(form, "min_weight"),
        "drawdown": [_pct_field(form, f"dd{i}") for i in (1, 2, 3)], "rebalance": form.get("rebalance") or "monthly",
        "benchmark": (form.get("benchmark") or "").strip() or BENCHMARKS[cls.market], "status": form.get("status") or "paper",
        "params": params,
    }


@router.post("/funds/save")
async def fund_save(request: Request):
    h = holder(request)
    app = h.app
    form = await request.form()
    original = (form.get("original_id") or "").strip()
    fund = store.fund(app.db, original) if original else None
    if original and fund is None:
        raise HTTPException(404, "没有这只基金")
    try:
        values = _parse_fund_form(form)
    except ValueError as e:
        return _form_page(request, {**DEFAULT_FORM, **{k: form.get(k) for k in ("id", "name")}}, fund, err=str(e), status_code=400)
    notes = []
    if fund:
        values["id"] = fund["id"]                      # id 不能改（库里的账户按 id 关联）
        if store.has_history(app.db, fund["id"]):      # 有历史的基金：市场、策略、初始资金不能改
            for k in ("market", "strategy", "style", "currency", "initial_capital"):
                if values[k] != fund[k]:
                    notes.append({"initial_capital": "初始资金不能改（请用「追加 / 取回资金」）", "strategy": "策略不能改（请新建基金）",
                                  "market": "市场不能改（请新建基金）"}.get(k, ""))
                values[k] = fund[k]
        held = len(store.positions(app.db, fund["id"]))
        if values["max_holdings"] < held:
            notes.append(f"现持有 {held} 只，超过新的上限 {values['max_holdings']}：不会立即卖出，下个调仓日卖出得分最低的")
    try:
        fcfg.FundDef.model_validate(values)
        cfg = yamledit.upsert_fund(fcfg.path(app), values, form.get("etag") or None, h.backup_dir, is_new=fund is None)
    except (ValueError, yamledit.EditError) as e:
        return _form_page(request, values, fund, err=str(e), status_code=400)
    msgs = store.sync(app.db, cfg.funds, today_local().isoformat())
    text = "；".join([m for m in msgs if m] + [n for n in notes if n])
    return back(f"/funds/{values['id']}", ok=f"已保存到 funds.yaml 并同步：{text}")


@router.post("/funds/{fund_id}/flow")
async def fund_flow(request: Request, fund_id: str):
    app = holder(request).app
    f = _fund_or_404(app, fund_id)
    form = await request.form()
    try:
        amount = _num(form.get("amount"), "金额")
    except ValueError as e:
        return back(f"/funds/{fund_id}", err=str(e))
    if amount is None:
        return back(f"/funds/{fund_id}", err="请填写金额")
    sign = -1 if form.get("direction") == "out" else 1
    _, _, equity = _holdings(app, f)
    nav = equity / store.units(app.db, f)
    try:
        row = store.add_flow(app.db, f, sign * amount, nav, today_local().isoformat(), (form.get("note") or "").strip()[:200] or None)
    except ValueError as e:
        return back(f"/funds/{fund_id}", err=str(e))
    return back(f"/funds/{fund_id}", ok=f"已{'追加' if sign > 0 else '取回'} {amount:,.0f} {f['currency']}：按单位净值 {nav:.4f} "
                f"{'申购' if sign > 0 else '赎回'} {abs(row['units']):,.0f} 份，净值与收益率不受影响")


@router.post("/funds/{fund_id}/delete")
async def fund_delete(request: Request, fund_id: str):
    h = holder(request)
    f = _fund_or_404(h.app, fund_id)
    form = await request.form()
    if form.get("confirm") != "yes":
        return back(f"/funds/{fund_id}", err="请先勾选确认")
    if store.has_history(h.app.db, fund_id):
        return back(f"/funds/{fund_id}", err="这只基金已有成交或净值记录，只能暂停（编辑 → 状态改为暂停），不能删除")
    try:
        cfg = fcfg.load(h.app)
        if cfg.fund(fund_id):
            yamledit.delete_fund(fcfg.path(h.app), fund_id, form.get("etag") or None, h.backup_dir)
    except (fcfg.FundsError, yamledit.EditError) as e:
        return back(f"/funds/{fund_id}", err=str(e))
    store.delete_fund(h.app.db, fund_id)
    return back("/funds", ok=f"已删除基金 {f['name']}（funds.yaml 已备份）")


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
        params_json=json.dumps(f["params"], ensure_ascii=False, indent=2), TRADE_ACTIONS=TRADE_ACTIONS,
        flows=store.flows(app.db, f["id"]), units=store.units(app.db, f), history=store.has_history(app.db, f["id"]),
        etag=yamledit.digest(fcfg.path(app).read_text(encoding="utf-8")) if fcfg.path(app).exists() else ""))


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
