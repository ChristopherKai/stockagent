"""选股页：两个池最近一次的候选（A / B / C 类分组）、送深研、主题 agent 的建议、事后检验。

页面只读数据库（事后检验也只用库里已有的日线）；运行选股、主题 agent 提交为后台任务。
送深研在本进程内写观察清单（与「新增公司」相同的写法），再提交「新公司初始化」任务。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import jobs
from ..screen import buckets, promote, store, themes
from ..screen import evaluate as sc_eval
from ..screen.events import LABEL as EVENT_LABEL
from ..screen.quick import VERDICT_LABEL
from . import yamledit
from .security import verify_csrf
from .server import page
from .views import back, holder, submit_job

router = APIRouter(dependencies=[Depends(verify_csrf)])


@router.get("/screen")
def screen_page(request: Request, pool: str = "long", run: int | None = None, show: str = "ABC"):
    app = holder(request).app
    if pool not in buckets.POOL_LABEL:
        raise HTTPException(404, "没有这个池")
    runs = store.runs(app.db, pool, 20)
    cur = next((r for r in runs if r["id"] == run), None) if run else next((r for r in runs if r["status"] == "done"), None)
    hits = store.hits(app.db, cur["id"]) if cur else []
    groups = [(t, [h for h in hits if h["tier"] == t]) for t in ("A", "B", "C", "D", "X") if t in show or show == "all"]
    watch = {l.key: c.id for c in app.watchlist.companies for l in c.listings}
    promoted = store.promoted(app.db)
    return page(request, "screen.html", pool=pool, runs=runs, cur=cur, groups=groups, show=show, watch=watch, promoted=promoted,
                counts={t: sum(1 for h in hits if h["tier"] == t) for t in ("A", "B", "C", "D", "X")},
                ideas=store.theme_ideas(app.db), POOL_LABEL=buckets.POOL_LABEL, TIER_LABEL=buckets.TIER_LABEL,
                BUCKET_LABEL=buckets.BUCKET_LABEL, VERDICT_LABEL=VERDICT_LABEL, EVENT_LABEL=EVENT_LABEL,
                cfg=app.settings.screen, busy=jobs.busy(app, "screen_run"))


@router.get("/screen/eval")
def screen_eval(request: Request):
    app = holder(request).app
    text = sc_eval.summarize(sc_eval.collect(app, fetch=False))
    return page(request, "screen_eval.html", text=text)


@router.post("/screen/run")
async def screen_run(request: Request):
    form = await request.form()
    pool = form.get("pool")
    flags = {"long": ["--long-only"], "event": ["--event-only"]}.get(pool, [])
    if form.get("no_llm"):
        flags.append("--no-llm")
    return submit_job(request, "screen_run", flags=flags)


@router.post("/screen/themes")
def screen_themes_job(request: Request):
    return submit_job(request, "screen_themes")


@router.post("/screen/promote/{hit_id}")
def screen_promote(request: Request, hit_id: int):
    h = holder(request)
    hit = store.hit(h.app.db, hit_id)
    if hit is None:
        raise HTTPException(404, "没有这个候选")
    try:
        cid, msg = promote.promote(h.app, hit["secid"], watchlist_path=h.watchlist_path)
    except (promote.PromoteError, yamledit.EditError) as e:
        return back(f"/screen?pool={hit['pool']}", err=str(e))
    h.reload()
    c = h.app.watchlist.get(cid)
    if c is not None and h.app.db.latest_dossier(c.id) is None:
        try:
            jid = jobs.submit(h.app, "onboard", [cid], source="web", note="选股器送深研")
        except jobs.JobError as e:
            return back(f"/screen?pool={hit['pool']}", err=f"{msg}；但提交初始化任务失败：{e}")
        return back(f"/jobs/{jid}", ok=f"{msg}；已提交「新公司初始化」（抓取 → 年报 → 分级 → 画像 → 深度报告 → 开始追踪）")
    return back(f"/c/{cid}", ok=msg)


@router.post("/screen/ideas/{idea_id}/{action}")
def screen_idea(request: Request, idea_id: int, action: str):
    app = holder(request).app
    try:
        if action == "accept":
            msg = themes.accept(app, idea_id)
        elif action == "dismiss":
            store.set_theme_idea(app.db, idea_id, "dismissed")
            msg = "已忽略这条建议"
        else:
            raise HTTPException(404)
    except (ValueError, yamledit.EditError) as e:
        return back("/screen", err=str(e))
    return back("/screen#ideas", ok=msg)
