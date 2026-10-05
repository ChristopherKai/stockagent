"""页面与操作：仪表盘、观察清单、标的详情（走势图、时间线、估值表）、修正详情、报告、事件、任务、状态。

页面只读数据库、不联网；耗时的操作（抓取、分级、深度报告、修正）提交为后台任务。
录入消息、补充数据、上传文档等自由文本输入在本进程内直接完成（不放进子进程的命令行参数）。
"""

from __future__ import annotations

import re
import tempfile
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from .. import app as appmod
from .. import anchor, chat, docs, jobs, monitor, newco, notify, scheduler, tracker
from .. import consensus as consensus_mod
from .. import dossier as dossier_mod
from ..config import Company
from ..docs import DOC_TYPES, SUPPORTED
from ..utils import CST, iso, lock_holder, parse_iso, today_local, utcnow
from ..valuation import SCENARIO_LABEL, _last_bar, _range_model, listing_views, load_model, risk_stats, stance_check, view_sort_key
from . import charts, yamledit
from .render import safe_url
from .security import SANDBOX_CSP, verify_csrf
from .server import page

router = APIRouter(dependencies=[Depends(verify_csrf)])
RANGES = {"90": 90, "180": 180, "365": 365, "all": None}


def holder(request: Request):
    return request.app.state.holder


def back(url: str, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if ok:
        url += f"{sep}ok={quote(ok[:300])}"
    elif err:
        url += f"{sep}err={quote(err[:300])}"
    return RedirectResponse(url, status_code=303)


def company_or_404(app, cid: str) -> Company:
    c = next((x for x in app.watchlist.companies if x.id == cid), None)
    if c is None:
        raise HTTPException(404, f"观察清单中没有「{cid}」")
    return c


def submit_job(request: Request, task: str, companies=None, flags=None, profile=None, to: str | None = None) -> RedirectResponse:
    """to：提交后回到哪个页面（默认进任务页）；列表页上的按钮用它留在原页。"""
    app = holder(request).app
    try:
        jid = jobs.submit(app, task, companies, flags, profile, source="web")
    except jobs.JobError as e:
        return back(to or request.headers.get("referer") or "/jobs", err=str(e))
    return back(to or f"/jobs/{jid}", ok=f"任务 #{jid} 已提交" if to else "任务已提交")


async def return_to(request: Request, default: str) -> str:
    """表单里的 next 字段：只接受本站内的路径（列表页上的按钮做完动作后回到列表）。"""
    n = str((await request.form()).get("next") or "")
    return n if n.startswith("/") and not n.startswith("//") and "\\" not in n and "\n" not in n else default


# ---------------------------------------------------------------- 仪表盘

PIN_PREFIX = "pinned:"


def _price_views(app, c: Company) -> list[dict]:
    """没有估值模型（还没有深度报告或情景价格）时：各上市地只有现价。"""
    out = []
    for l in c.listings:
        price, day = _last_bar(app, l)
        out.append({"listing": l, "key": l.key, "label": l.display, "currency": l.currency, "price": price, "price_date": day,
                    "primary": l.key == c.pricing_listing.key, "multi": len(c.listings) > 1, "convertible": True, "target": None, "upside": None,
                    "scenarios": {}, "stats": None, "stance": None, "reason": "", "premium": None})
    return out


def watch_rows(app) -> list[dict]:
    """观察清单与仪表盘共用的行：每家公司的追踪状态、各上市地视图（现价、目标价、情景价、立场）。
    排序：置顶在前；然后按观点（偏多在前）、同观点按空间、再按上行/下行比（多空比）从高到低；没有观点的在最后。"""
    pins = app.db.kv_prefix(PIN_PREFIX)
    rows = []
    for c in app.watchlist.companies:
        d = app.db.latest_dossier(c.id)
        t = app.db.tracker(c.id)
        s = tracker.status(app, c)
        views = (s["views"] if s else (tracker.card_views(app, c, d) if d else [])) or _price_views(app, c)
        views = sorted(views, key=lambda v: (view_sort_key([v]), not v["primary"]))
        rows.append({"company": c, "dossier": d, "tracker": t, "s": s, "views": views, "pinned": PIN_PREFIX + c.id in pins,
                     "enabled": bool(s and s["enabled"]), "report_stance": d["card"].get("stance") if d else None,
                     "thesis": d["card"].get("thesis") if d else None,
                     "thesis_status": s["revision"]["thesis_status"] if s else None,
                     "key": view_sort_key(views), "report_at": d["created_at"] if d else None})
    rows.sort(key=lambda r: (not r["pinned"], r["key"], r["company"].name))
    return rows


@router.get("/")
def dashboard(request: Request):
    app = holder(request).app
    all_rows = watch_rows(app)
    rows = [r for r in all_rows if r["enabled"]]
    untracked = [{"company": r["company"], "dossier": r["dossier"] is not None} for r in all_rows if not r["enabled"]]
    names = {c.id: c.name for c in app.watchlist.companies}
    alerts = [(names.get(c.id, c.id), t) for c in app.watchlist.companies for t in app.db.triggers(c.id) if t["status"] != "ok"]
    catalysts = sorted(((names[c.id], x) for c in app.watchlist.companies for x in monitor.upcoming(app, c, days=30)),
                       key=lambda p: p[1]["due_date"])
    return page(request, "dashboard.html", rows=rows, untracked=untracked, names=names, alerts=alerts, catalysts=catalysts,
                revisions=app.db.recent_revisions(12, kinds=("revision", "review")),
                requests=app.db.requests(status="open"), active=app.db.jobs(20, statuses=("queued", "running")),
                recent_jobs=app.db.jobs(6), schedule=scheduler.next_runs(app),
                llm_reason=app.llm.unavailable_reason("tracker"))


# ---------------------------------------------------------------- 观察清单

@router.get("/companies")
def companies(request: Request):
    app = holder(request).app
    _, etag = yamledit.load(holder(request).watchlist_path)
    return page(request, "companies.html", rows=watch_rows(app), etag=etag)


@router.post("/companies/{cid}/pin")
async def company_pin(request: Request, cid: str):
    app = holder(request).app
    c = company_or_404(app, cid)
    if (await request.form()).get("pinned") == "1":
        app.db.kv_set(PIN_PREFIX + cid, iso(utcnow()))
        msg = f"已置顶 {c.name}"
    else:
        app.db.kv_delete(PIN_PREFIX + cid)
        msg = f"已取消置顶 {c.name}"
    return back(await return_to(request, "/companies"), ok=msg)


def _lines(text: str | None) -> list[str]:
    return [x.strip() for x in (text or "").splitlines() if x.strip()]


def _handles(text: str | None) -> list[str]:
    return [x for x in re.split(r"[\s,，、]+", text or "") if x]


def _company_from_form(form, cid: str | None = None) -> dict:
    """表单 → 观察清单条目（经 Company 校验，代码按市场规范化，如港股补足 5 位）。"""
    listings = []
    for i in range(6):
        code = (form.get(f"listing{i}_code") or "").strip()
        if not code:
            continue
        market = form.get(f"listing{i}_market") or "HK"
        exchange = (form.get(f"listing{i}_exchange") or "").strip() or None
        if market == "US" and exchange not in ("NASDAQ", "NYSE", "AMEX"):
            raise ValueError(f"美股 {code} 需要选择交易所（NASDAQ / NYSE / AMEX）")
        ratio = (form.get(f"listing{i}_ads_ratio") or "").strip()
        try:
            ads_ratio = float(ratio) if ratio and market == "US" else None
        except ValueError:
            raise ValueError(f"美股 {code} 的 ADS 换股比例不是数字：{ratio}") from None
        listings.append({"market": market, "code": code, "exchange": exchange,
                         "benchmark": (form.get(f"listing{i}_benchmark") or "").strip() or None, "ads_ratio": ads_ratio})
    data = {
        "id": cid or (form.get("id") or "").strip(), "name": (form.get("name") or "").strip(),
        "name_en": (form.get("name_en") or "").strip() or None,
        "aliases": [a.strip() for a in re.split(r"[,，、\n]", form.get("aliases") or "") if a.strip()],
        "listings": listings, "pricing": (form.get("pricing") or "").strip() or None,
        "sector": (form.get("sector") or "").strip() or None,
        "notes": (form.get("notes") or "").strip().replace("\r\n", "\n") or None,
        "watch_items": _lines(form.get("watch_items")),
        "news_queries": {loc: _lines(form.get(f"q_{loc}")) for loc in ("zh-CN", "zh-HK", "en-US") if _lines(form.get(f"q_{loc}"))},
        "twitter_official": _handles(form.get("twitter_official")), "twitter_accounts": _handles(form.get("twitter_accounts")),
    }
    c = Company.model_validate(data)  # 抛出 ValueError（含 pydantic 校验错误）
    data["twitter_official"], data["twitter_accounts"] = c.twitter_official, c.twitter_accounts  # 已去掉 @ 并去重
    data["listings"] = [{"market": l.market, "code": l.code, "exchange": l.exchange if l.market != "HK" else None,
                         "benchmark": l.benchmark, "ads_ratio": l.ads_ratio} for l in c.listings]
    return data


@router.get("/companies/new")
def company_new(request: Request):
    _, etag = yamledit.load(holder(request).watchlist_path)
    return page(request, "company_form.html", company=None, etag=etag, is_new=True)


@router.get("/companies/{cid}/edit")
def company_edit(request: Request, cid: str):
    c = company_or_404(holder(request).app, cid)
    _, etag = yamledit.load(holder(request).watchlist_path)
    return page(request, "company_form.html", company=c, etag=etag, is_new=False)


async def _save_company(request: Request, cid: str | None):
    h = holder(request)
    form = await request.form()
    is_new = cid is None
    try:
        data = _company_from_form(form, cid)
        if not data["listings"]:
            raise ValueError("至少填写一个上市地")
        yamledit.upsert_company(h.watchlist_path, data, form.get("etag"), h.backup_dir, is_new=is_new)
        h.reload()
    except (ValueError, yamledit.EditError) as e:
        c = company_or_404(h.app, cid) if cid else None
        return page(request, "company_form.html", status_code=400, company=c, etag=form.get("etag"), is_new=is_new,
                    error=str(e), form=form)
    if is_new:
        (h.app.data_dir / "inbox" / data["id"]).mkdir(parents=True, exist_ok=True)
        return back(f"/companies/{data['id']}/edit", ok=f"已添加 {data['name']}：可以提交「新公司初始化」任务，"
                                                           "依次抓取资料、下载年报、分级、画像、生成深度报告并开始追踪")
    return back("/companies", ok=f"已保存 {data['name']}")


@router.post("/companies/new")
async def company_create(request: Request):
    return await _save_company(request, None)


@router.post("/companies/{cid}/edit")
async def company_update(request: Request, cid: str):
    company_or_404(holder(request).app, cid)
    return await _save_company(request, cid)


@router.post("/companies/{cid}/delete")
async def company_delete(request: Request, cid: str):
    h = holder(request)
    c = company_or_404(h.app, cid)
    form = await request.form()
    try:
        yamledit.delete_company(h.watchlist_path, cid, form.get("etag"), h.backup_dir)
        h.reload()
    except yamledit.EditError as e:
        return back("/companies", err=str(e))
    return back("/companies", ok=f"已从观察清单移除 {c.name}（数据库里的历史数据保留，重新添加同一 id 即可恢复）")


@router.post("/companies/quick")
async def companies_quick(request: Request):
    """只填名称或代码（可选市场）：提交后台任务，联网识别并查公司资料写入观察清单，再跑完整的初始化（页面进程不联网）。"""
    app = holder(request).app
    form = await request.form()
    market, query = (form.get("market") or "").strip(), (form.get("code") or "").strip()
    try:
        if market:
            market, query = newco.normalize(market, query)
        else:
            query = newco.check_query(query)
            if hit := newco.infer(query):
                market, query = hit
    except ValueError as e:
        return back("/companies", err=str(e))
    c = newco.existing(app, market, query) if market else newco.existing_name(app, query)
    if c:
        return back("/companies", err=f"{query} 已在观察清单中：{c.name}")
    try:
        jid = jobs.submit(app, "add_company", listing=(market, query), source="web")
    except jobs.JobError as e:
        return back("/companies", err=str(e))
    return back(f"/jobs/{jid}", ok=f"已提交：识别「{query}」并加入观察清单，随后自动初始化（深度报告每家数分钟）")


@router.post("/companies/track")
async def companies_track(request: Request):
    """批量开始 / 停止追踪。已有深度报告的公司在本进程内用代码建立基线；没有报告的提交任务先生成报告。"""
    app = holder(request).app
    form = await request.form()
    ids = form.getlist("ids")
    action = form.get("action")
    msgs, need_dossier = [], []
    for cid in ids:
        c = company_or_404(app, cid)
        if action == "untrack":
            msgs.append(tracker.remove(app, c))
        elif app.db.latest_dossier(c.id) is None:
            need_dossier.append(c.id)
        else:
            try:
                msgs.append(tracker.add(app, c, origin="web").split("\n")[0])
            except (tracker.TrackerError, ValueError) as e:
                msgs.append(f"✗ {e}")
    if need_dossier:
        try:
            jid = jobs.submit(app, "target_add", need_dossier, ["--dossier"], source="web")
            msgs.append(f"{len(need_dossier)} 家还没有深度报告：已提交任务 #{jid}（先生成报告再开始追踪）")
        except jobs.JobError as e:
            msgs.append(f"✗ {e}")
    return back("/companies", ok="；".join(msgs) or "没有选择公司")


# ---------------------------------------------------------------- 标的详情

def _timeline(app, c: Company, since: str | None) -> list[dict]:
    since = since or "0000"
    refs = app.db.refs(c.id, "item:")
    out = []
    for e in app.db.items_since(c.id, since, min_materiality=2):
        ref = refs.get(f"item:{e['uid']}")
        out.append({"type": "event", "at": e["published_at"], "e": e, "ref": ref})
    for r in app.db.revisions(c.id):
        if r["created_at"] >= since:
            out.append({"type": "revision", "at": r["created_at"], "r": r})
    for d in app.db.dossiers(c.id):
        if d["created_at"] >= since:
            out.append({"type": "dossier", "at": d["created_at"], "d": d})
    out.sort(key=lambda x: x["at"], reverse=True)
    return out


def _compare(model, base) -> list[dict]:
    """当前模型与基线逐情景对比（驱动、倍数、概率、股价）。"""
    rows = []
    probs = model.probabilities()
    bprobs = base.probabilities() if base else {}
    for s in model.scenarios:
        b = base.scenario(s.name) if base else None
        rows.append({"name": s.name, "label": SCENARIO_LABEL[s.name], "s": s, "b": b, "price": model.price(s),
                     "bprice": base.price(b) if b else None, "prob": probs[s.name], "bprob": bprobs.get(s.name)})
    return rows


@router.get("/c/{cid}")
def company_page(request: Request, cid: str, span: str = Query("180", alias="range")):
    app = holder(request).app
    c = company_or_404(app, cid)
    days = RANGES.get(span, 180)
    since = iso(utcnow() - timedelta(days=days)) if days else None
    s = tracker.status(app, c)
    model = s["live_model"] if s else None
    listing = next((l for l in c.listings if model and l.key == model.listing), c.pricing_listing)
    bars = app.bars(app.secid(listing), limit=4000)
    if bars is not None and since:
        bars = bars[bars["date"] >= since[:10]]
    revs = sorted(app.db.revisions(c.id), key=lambda r: r["id"])
    chart_revs = [{**r, "label": tracker.KIND_LABEL[r["kind"]], "changed": r["kind"] != "review"} for r in revs]
    events = [{"published_at": e["published_at"], "materiality": e["materiality"], "direction": e["direction"],
               "uid": e["uid"], "title": e["summary_zh"] or e["title"]}
              for e in app.db.items_since(c.id, since or "0000", min_materiality=app.settings.tracker.min_materiality)]
    chart = charts.timeline_svg(bars, chart_revs, events, model.price_currency if model else listing.currency)
    base_rev = s["baseline"] if s else None
    d = app.db.latest_dossier(c.id)
    cat_status = {}
    for r in revs:
        for x in r["catalysts"] or []:
            cat_status[x["ref"]] = x
    cats = tracker.catalyst_snapshot(app, c, d) if d else []
    for x in cats:
        x["assessed"] = cat_status.get(x["ref"])
    return page(request, "company.html", c=c, s=s, model=model, compare=_compare(model, load_model(base_rev["model"]) if base_rev else None) if model else [],
                chart=chart, span=span, timeline=_timeline(app, c, since), dossier=d, triggers=app.db.triggers(c.id),
                catalysts=cats, requests=app.db.requests(c.id), plan=app.db.plan(c.id) or {},
                doc_types=DOC_TYPES, supported=sorted(SUPPORTED), confirmed=tracker.confirmed_refs(app, c),
                views=(s["views"] if s else (tracker.card_views(app, c, d) if d else [])) or _price_views(app, c),
                earnings=app.db.earnings(c.id, 6), snapshots=_snapshots(app, c), today=today_local(),
                stance_now=monitor.current_stance(app, c),
                pending=[(r, i, x) for r in revs if r["id"] > (base_rev["id"] if base_rev else 0)
                         for i, x in enumerate(r["rejected"] or []) if x.get("confirm")])


def _snapshots(app, c: Company) -> list[dict]:
    """一致预期快照（每个来源、财年、指标的最新一份），附展示用的文字。"""
    return [{**r, "label": consensus_mod.SNAPSHOT_LABEL.get(r["source"], r["source"]),
             "metric_label": consensus_mod.METRIC_LABEL.get(r["metric"], r["metric"]) + ("（每 ADS）" if r["per_ads"] else ""),
             "display": consensus_mod.show(r["metric"], r["value"], r["currency"] or "CNY")}
            for r in app.db.consensus_latest(c.id)]


@router.post("/c/{cid}/consensus")
async def company_consensus(request: Request, cid: str):
    """录入一致预期（港股没有免费数据源）。自由文本在本进程内处理。"""
    app = holder(request).app
    c = company_or_404(app, cid)
    form = await request.form()
    try:
        value = float(str(form.get("value") or "").replace(",", ""))
        year = int(str(form.get("year") or "0").strip())
        n_raw = str(form.get("n") or "").strip()
        msg = consensus_mod.save_manual(app, c, year, str(form.get("metric") or ""), value, str(form.get("currency") or ""),
                                        str(form.get("date") or "").strip() or None, int(n_raw) if n_raw else None,
                                        str(form.get("source") or "").strip() or None)
    except ValueError as e:
        return back(f"/c/{cid}", err=f"一致预期未保存：{e}")
    return back(f"/c/{cid}", ok=msg)


@router.post("/c/{cid}/consensus/{rid}/delete")
def company_consensus_delete(request: Request, cid: str, rid: int):
    app = holder(request).app
    company_or_404(app, cid)
    if app.db.delete_consensus(cid, rid):
        return back(f"/c/{cid}", ok="已删除这条一致预期")
    return back(f"/c/{cid}", err="没有这条录入（程序记录的快照不能删除）")


@router.post("/c/{cid}/track")
async def company_track(request: Request, cid: str):
    app = holder(request).app
    c = company_or_404(app, cid)
    to = await return_to(request, f"/c/{cid}")
    if app.db.latest_dossier(cid) is None:
        return submit_job(request, "target_add", [cid], ["--dossier"], to=to if to != f"/c/{cid}" else None)
    try:
        msg = tracker.add(app, c, origin="web")
    except (tracker.TrackerError, ValueError) as e:
        return back(to, err=str(e))
    return back(to, ok=msg.replace("\n", " "))


@router.post("/c/{cid}/untrack")
async def company_untrack(request: Request, cid: str):
    app = holder(request).app
    return back(await return_to(request, f"/c/{cid}"), ok=tracker.remove(app, company_or_404(app, cid)))


@router.post("/c/{cid}/rebase")
def company_rebase(request: Request, cid: str):
    app = holder(request).app
    c = company_or_404(app, cid)
    try:
        rev = tracker.rebase(app, c, origin="web")
    except (tracker.TrackerError, ValueError) as e:
        return back(f"/c/{cid}", err=str(e))
    return back(f"/c/{cid}", ok=f"已用深度报告 #{rev['dossier_id']} 重建估值基线")


@router.post("/c/{cid}/check")
async def company_check(request: Request, cid: str):
    company_or_404(holder(request).app, cid)
    to = await return_to(request, "")
    return submit_job(request, "target_run", [cid], to=to or None)


@router.post("/c/{cid}/dossier")
async def company_dossier(request: Request, cid: str):
    company_or_404(holder(request).app, cid)
    return submit_job(request, "dossier", [cid], to=await return_to(request, "") or None)


@router.post("/c/{cid}/onboard")
def company_onboard(request: Request, cid: str):
    company_or_404(holder(request).app, cid)
    return submit_job(request, "onboard", [cid])


@router.post("/c/{cid}/confirm/{rid}/{index}")
def company_confirm(request: Request, cid: str, rid: int, index: int):
    app = holder(request).app
    c = company_or_404(app, cid)
    try:
        rev = tracker.confirm_change(app, c, rid, index, origin="web")
    except tracker.TrackerError as e:
        return back(f"/c/{cid}", err=str(e))
    return back(f"/c/{cid}#rev-{rev['id']}", ok=rev["summary"])


@router.post("/c/{cid}/note")
async def company_note(request: Request, cid: str):
    app = holder(request).app
    c = company_or_404(app, cid)
    form = await request.form()
    title = (form.get("title") or "").strip()
    url = (form.get("url") or "").strip() or None
    if not title:
        return back(f"/c/{cid}", err="消息标题不能为空")
    if url and not safe_url(url):
        return back(f"/c/{cid}", err="链接只能是 http(s) 地址")
    msg = appmod.add_manual_item(app, c, title[:300], (form.get("detail") or "").strip()[:2000], url)
    return back(f"/c/{cid}", ok=msg + "（下次分级时与新闻一起处理）")


@router.post("/c/{cid}/data")
async def company_data(request: Request, cid: str):
    app = holder(request).app
    company_or_404(app, cid)
    form = await request.form()
    metric, period = (form.get("metric") or "").strip(), (form.get("period") or "").strip()
    try:
        value = float(str(form.get("value") or "").replace(",", ""))
    except ValueError:
        return back(f"/c/{cid}", err="数值不合法")
    if not metric or not period:
        return back(f"/c/{cid}", err="指标与期间不能为空")
    scope = "" if form.get("scope") == "industry" else cid
    app.db.upsert_manual(scope, metric[:100], period[:40], value, (form.get("unit") or "").strip() or None,
                         (form.get("note") or "").strip() or None, "manual:web")
    return back(f"/c/{cid}", ok=f"已保存：{metric} {period} = {value:g}（下次修正时作为新依据）")


@router.post("/c/{cid}/upload")
async def company_upload(request: Request, cid: str):
    app = holder(request).app
    c = company_or_404(app, cid)
    form = await request.form()
    f = form.get("file")
    if f is None or not getattr(f, "filename", ""):
        return back(f"/c/{cid}", err="请选择文件")
    suffix = Path(f.filename).suffix.lower()
    if suffix not in SUPPORTED:
        return back(f"/c/{cid}", err=f"不支持的文件类型 {suffix}（支持：{'、'.join(sorted(SUPPORTED))}）")
    doc_type = form.get("doc_type") or "other"
    if doc_type not in DOC_TYPES:
        doc_type = "other"
    limit = app.settings.web.upload_max_mb * 1024 * 1024
    tmp_dir = app.data_dir / "tmp" / "uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w.\-]+", "_", Path(f.filename).stem)[:60] or "upload"
    with tempfile.TemporaryDirectory(dir=tmp_dir) as d:
        path = Path(d) / f"{stem}{suffix}"
        size = 0
        with open(path, "wb") as out:
            while chunk := await f.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    return back(f"/c/{cid}", err=f"文件超过 {app.settings.web.upload_max_mb} MB")
                out.write(chunk)
        period = (form.get("period") or "").strip() or docs.infer_period(f.filename, doc_type)
        doc, new = docs.store_document(app.db, app.data_dir, c.id, path, doc_type=doc_type, period=period,
                                       title=(form.get("title") or "").strip() or Path(f.filename).stem, source="manual")
    if not new:
        return back(f"/c/{cid}", ok=f"已存在相同文件：#{doc['id']} {doc['title']}")
    warn = "（几乎没有抽取到文字，可能是扫描件）" if (doc["chars"] or 0) < 1000 and suffix == ".pdf" else ""
    return back(f"/c/{cid}", ok=f"已入库：#{doc['id']} {DOC_TYPES[doc_type]} {doc['period'] or ''}《{doc['title']}》"
                                f"{doc['pages'] or '-'} 页、{doc['chars']:,} 字{warn}；下次修正时作为新依据")


@router.post("/requests/{rid}/close")
async def request_close(request: Request, rid: int):
    app = holder(request).app
    form = await request.form()
    status = "dismissed" if form.get("status") == "dismissed" else "done"
    ok = app.db.close_request(rid, status)
    return back(request.headers.get("referer") or "/", ok="已关闭请求" if ok else None, err=None if ok else "没有这个未关闭的请求")


IMPACT_ORDER = {"material": 0, "minor": 1, "unassessed": 2, "none": 3}


def _evidence(events: list[dict] | None) -> list[dict]:
    """修正依据的展示结构：元信息（时间、来源、重要性、方向、类型）、标题、带标签的字段（预期、待跟踪、发布后）。
    排序：先按影响（实质 → 轻微 → 未评估 → 不影响），再按重要性从高到低（证伪条件升级视同最高），最后按时间从新到旧。"""
    out = []
    for e in events or []:
        parts = [x.strip() for x in (e.get("text") or "").split("｜") if x.strip()]
        body = parts[1:] if len(parts) > 1 else parts
        if e.get("kind") == "event" and len(body) >= 6:
            meta, title, rest = body[:5], body[5], body[6:]
        else:
            meta, title, rest = [], (body[0] if body else ""), body[1:]
        fields = {}
        for x in rest:
            k, sep, v = x.partition("：")
            if sep and len(k) <= 6:
                if k != "原文":
                    fields[k] = v
            else:
                title += "｜" + x
        weight = 5 if e.get("kind") == "trigger" else (e.get("materiality") or 0)
        out.append({**e, "meta": [m for m in meta if not m.startswith("重要性")], "title": title, "weight": weight,
                    "reaction": fields.pop("发布后", None), "extra": list(fields.items())})
    out.sort(key=lambda x: x["meta"][0] if x["meta"] else "", reverse=True)
    out.sort(key=lambda x: (IMPACT_ORDER.get(x.get("impact"), 2), -x["weight"]))
    return out


def _flags(flags: list[str] | None) -> list[dict]:
    """提示与待确认问题：「类别：标题——说明」拆开展示。"""
    out = []
    for f in flags or []:
        head, _, detail = f.partition("——")
        tag, sep, title = head.partition("：")
        if not sep or len(tag) > 10:
            tag, title = "", head
        out.append({"tag": tag, "title": title, "detail": detail})
    return out


@router.get("/c/{cid}/revisions/{rid}")
def revision_page(request: Request, cid: str, rid: int):
    app = holder(request).app
    c = company_or_404(app, cid)
    r = app.db.revision(rid)
    if r is None or r["company_id"] != cid:
        raise HTTPException(404, "没有这条修正记录")
    prev = next((x for x in app.db.revisions(cid) if x["id"] < rid), None)
    model = load_model(r["model"])
    prev_model = load_model(prev["model"]) if prev else None
    evidence = _evidence(r["events"])
    st = risk_stats(model, r["price"])
    base = model.scenario("base")
    return page(request, "revision.html", c=c, r=r, model=model, prev_model=prev_model, compare=_compare(model, prev_model),
                st=st, sc=stance_check(r["stance"], st, app.settings.tracker.stance_band),
                ac=anchor.check(model.anchor, model.price(base), app.settings.tracker.anchor_band) if base else None,
                evidence=[e for e in evidence if e.get("impact") != "none"], unaffected=[e for e in evidence if e.get("impact") == "none"],
                confirmed=tracker.confirmed_refs(app, c), flags=_flags(r["flags"]),
                chats=[x for x in app.db.chats(cid) if x["revision_id"] == rid], quick=chat.QUICK_REVISION,
                chat_reason=app.llm.unavailable_reason("chat"), chat_model=app.llm.describe("chat"))


@router.get("/c/{cid}/report")
def report_page(request: Request, cid: str, did: int | None = None):
    """当前版本：原深度报告 + 之后的修正记录 + 最新估值表（原报告不改动，修正以增量记录呈现）。"""
    app = holder(request).app
    c = company_or_404(app, cid)
    d = app.db.dossier(did) if did else app.db.latest_dossier(cid)
    if d is None or d["company_id"] != cid:
        raise HTTPException(404, "还没有深度报告")
    s = tracker.status(app, c)
    revs = [r for r in app.db.revisions(cid) if r["dossier_id"] == d["id"]]
    return page(request, "report.html", c=c, d=d, s=s, revisions=revs, versions=app.db.dossiers(cid),
                baseline=next((r for r in reversed(revs) if r["kind"] == "baseline"), None),
                chats=[x for x in app.db.chats(cid) if x["dossier_id"] == d["id"] and not x["revision_id"]], quick=chat.QUICK_QUESTIONS,
                chat_reason=app.llm.unavailable_reason("chat"), chat_model=app.llm.describe("chat"))


def utc_day(ts: str) -> str:
    """UTC 时间戳 → 北京时间的日期（YYYY-MM-DD）。"""
    return parse_iso(ts).astimezone(CST).date().isoformat()


def _bar_close_on(app, listing, day: str) -> float | None:
    """day（含）之前最近一个交易日的收盘价（只读库）。"""
    bars = app.bars(app.secid(listing), limit=4000)
    if bars is None:
        return None
    older = bars[bars["date"] <= day]
    return float(older["close"].iloc[-1]) if len(older) else None


def _history(app, c: Company) -> list[dict]:
    """每一版深度报告的摘要：报告日价格、立场、情景价格（跟踪卡区间中值）、情景加权目标价与空间、至今涨跌。"""
    tr = app.db.tracker(c.id)
    baselines = {r["dossier_id"]: r for r in app.db.revisions(c.id) if r["kind"] == "baseline"}
    mode = app.settings.tracker.target
    rows = []
    for i, d in enumerate(app.db.dossier_history(c.id)):
        card = d["card"]
        listing = next((l for l in c.listings if l.key == card.get("price_listing")), c.listings[0])
        day = utc_day(d["created_at"])
        price = (card.get("stats") or {}).get("price") or (baselines.get(d["id"]) or {}).get("price") or _bar_close_on(app, listing, day)
        scen = [x for x in card.get("scenarios", []) if x.get("name") in SCENARIO_LABEL and x.get("price_low") and x.get("price_high")]
        mids = {x["name"]: (x["price_low"] + x["price_high"]) / 2 for x in scen}
        target = _range_model(scen, listing.key, listing.currency, "").target(mode) if scen else None
        now, _ = _last_bar(app, listing)
        rows.append({"d": d, "day": day, "listing": listing, "price": price, "stance": card.get("stance"), "thesis": card.get("thesis"),
                     "mids": mids, "probs": {x["name"]: x.get("probability") for x in scen}, "target": target,
                     "upside": target / price - 1 if target and price else None, "now": now,
                     "since": now / price - 1 if now and price else None, "latest": i == 0,
                     "baseline": bool(tr and tr["enabled"] and tr["dossier_id"] == d["id"]), "revisions": d["id"] in baselines})
    return rows


@router.get("/c/{cid}/reports")
def company_reports(request: Request, cid: str):
    app = holder(request).app
    c = company_or_404(app, cid)
    return page(request, "company_reports.html", c=c, rows=_history(app, c), mode=app.settings.tracker.target)


@router.post("/c/{cid}/reports/{did}/delete")
def company_report_delete(request: Request, cid: str, did: int):
    app = holder(request).app
    c = company_or_404(app, cid)
    try:
        msg = dossier_mod.delete_version(app, c, did)
    except ValueError as e:
        return back(f"/c/{cid}/reports", err=str(e))
    return back(f"/c/{cid}/reports", ok=msg)


# ---------------------------------------------------------------- 事件与文件

@router.get("/events")
def events_page(request: Request, company: str = "", days: int = 7, min_m: int = Query(3, alias="min"), direction: str = ""):
    app = holder(request).app
    days = max(1, min(days, 365))
    since = iso(utcnow() - timedelta(days=days))
    rows = []
    for c in app.watchlist.companies:
        if company and c.id != company:
            continue
        refs = app.db.refs(c.id, "item:")
        for e in app.db.items_since(c.id, since, min_materiality=max(1, min_m)):
            if direction and e["direction"] != direction:
                continue
            rows.append((c, e, refs.get(f"item:{e['uid']}")))
    rows.sort(key=lambda x: x[1]["published_at"], reverse=True)
    return page(request, "events.html", rows=rows[:500], company=company, days=days, min_m=min_m, direction=direction)


FILE_RE = re.compile(r"^[\w\-.]+\.(html|pdf|md)$")


@router.get("/reports")
def reports_page(request: Request):
    app = holder(request).app
    rep_dir = app.data_dir / "reports"
    briefs = sorted((p for p in rep_dir.glob("*.html") if p.name != "latest.html"), reverse=True)[:60] if rep_dir.exists() else []
    names = {c.id: c.name for c in app.watchlist.companies}
    dossiers = [(names.get(d["company_id"], d["company_id"]), d, Path(d["html_path"]).name if d["html_path"] else None,
                 Path(d["pdf_path"]).name if d["pdf_path"] else None) for d in app.db.dossiers()]
    return page(request, "reports.html", briefs=[p.name for p in briefs], dossiers=dossiers)


@router.get("/files/{kind}/{name}")
def files(request: Request, kind: str, name: str):
    """简报与深度报告文件：只允许 data/reports、data/dossiers 下的文件名；HTML 加 sandbox CSP（报告里的 HTML 未经净化）。"""
    app = holder(request).app
    if kind not in ("reports", "dossiers") or not FILE_RE.match(name):
        raise HTTPException(404, "文件不存在")
    base = (app.data_dir / kind).resolve()
    path = (base / name).resolve()
    if path.parent != base or not path.is_file():
        raise HTTPException(404, "文件不存在")
    headers = {"Content-Security-Policy": SANDBOX_CSP} if path.suffix in (".html", ".md") else {}
    media = {"html": "text/html; charset=utf-8", "pdf": "application/pdf", "md": "text/plain; charset=utf-8"}[path.suffix[1:]]
    return FileResponse(path, media_type=media, headers=headers)


# ---------------------------------------------------------------- 任务

@router.get("/jobs")
def jobs_page(request: Request):
    app = holder(request).app
    return page(request, "jobs.html", jobs=app.db.jobs(100), tasks=jobs.TASKS, profiles=list(app.settings.llm.profiles),
                dossier_pid=lock_holder(app.lock_path("dossier")), pipeline_pid=lock_holder(app.lock_path("pipeline")))


@router.post("/jobs")
async def jobs_submit(request: Request):
    form = await request.form()
    return submit_job(request, form.get("task") or "", form.getlist("companies"), form.getlist("flags"), form.get("profile") or None)


@router.get("/jobs/{jid}")
def job_page(request: Request, jid: int):
    app = holder(request).app
    j = app.db.job(jid)
    if j is None:
        raise HTTPException(404, "没有这个任务")
    text, offset = jobs.read_log(j["log_path"])
    return page(request, "job.html", j=j, log=text, offset=offset)


@router.get("/jobs/{jid}/log")
def job_log(request: Request, jid: int, offset: int = 0):
    app = holder(request).app
    j = app.db.job(jid)
    if j is None:
        raise HTTPException(404, "没有这个任务")
    text, new = jobs.read_log(j["log_path"], max(0, offset))
    return JSONResponse({"text": text, "offset": new, "status": j["status"], "exit_code": j["exit_code"]})


@router.post("/jobs/{jid}/cancel")
def job_cancel(request: Request, jid: int):
    ok = request.app.state.runner.cancel(jid)
    return back(f"/jobs/{jid}", ok="已请求取消" if ok else None, err=None if ok else "任务已结束，无法取消")


# ---------------------------------------------------------------- 状态

@router.get("/status")
def status_page(request: Request):
    h = holder(request)
    app = h.app
    since = iso(utcnow() - timedelta(days=7))
    breakers = []
    for key, at in app.db.kv_prefix("down:").items():
        age = (utcnow() - parse_iso(at)).total_seconds() / 60
        breakers.append({"source": key[5:], "at": at, "active": age < appmod.DOWN_MINUTES})
    locks = {name: lock_holder(app.lock_path(name)) for name in ("dossier", "pipeline", "web")}
    return page(request, "status.html", stats=app.db.stats(), breakers=breakers, usage=app.db.llm_usage_by(since),
                channels=notify.channels(app.settings), missing=notify.missing(app.settings), locks=locks, config_path=h.config_path,
                tasks={t: (app.llm.describe(t), app.llm.unavailable_reason(t)) for t in
                       ("triage", "stock_brief", "market_brief", "profile", "research", "dossier", "monitor", "tracker", "chat")},
                today=date.today())


@router.post("/status/breaker/{source}/reset")
def breaker_reset(request: Request, source: str):
    app = holder(request).app
    app.db.kv_delete(f"down:{source}")
    return back("/status", ok=f"已复位 {source}：下次抓取会重新尝试")


@router.post("/status/push-test")
def push_test(request: Request):
    app = holder(request).app
    res = notify.push(app.settings, "【stockagent 测试推送】", "这是一条来自 WebUI 的测试消息。")
    return back("/status", ok="；".join(res) if res else None, err=None if res else "未配置推送渠道（在「密钥」页填写 webhook）")
