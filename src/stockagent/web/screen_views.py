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

# 结果页的范围筛选（只过滤已有候选，不重新运行）：键 → (标签, 判断)
SCOPES = {
    "all": ("全部", lambda h: True),
    "hkc": ("港股通", lambda h: h["market"] == "HK" and in_connect(h)),
    "hko": ("非港股通港股", lambda h: h["market"] == "HK" and not in_connect(h)),
    "hk": ("全部港股", lambda h: h["market"] == "HK"),
    "a": ("A 股", lambda h: h["market"] == "A"),
}
HK_SCOPE_LABEL = {"all": "全部港股", "connect": "仅港股通"}


def in_connect(h: dict) -> bool:
    """候选是否在港股通内；没有标记的旧运行只含港股通标的。"""
    return bool((h.get("metrics", {}).get("info") or {}).get("in_connect", True))


@router.get("/screen")
def screen_page(request: Request, pool: str = "long", run: int | None = None, show: str = "ABC", scope: str = "all"):
    app = holder(request).app
    if pool not in buckets.POOL_LABEL:
        raise HTTPException(404, "没有这个池")
    runs = store.runs(app.db, pool, 20)
    cur = next((r for r in runs if r["id"] == run), None) if run else next((r for r in runs if r["status"] == "done"), None)
    hits = store.hits(app.db, cur["id"]) if cur else []
    if scope not in SCOPES:
        raise HTTPException(404, "没有这个范围")
    n_all = len(hits)
    hits = [h for h in hits if SCOPES[scope][1](h)]
    groups = [(t, [h for h in hits if h["tier"] == t]) for t in ("A", "B", "C", "D", "X") if t in show or show == "all"]
    watch = {l.key: c.id for c in app.watchlist.companies for l in c.listings}
    promoted = store.promoted(app.db)
    return page(request, "screen.html", pool=pool, runs=runs, cur=cur, groups=groups, show=show, watch=watch, promoted=promoted,
                scope=scope, scopes={k: v[0] for k, v in SCOPES.items()}, n_hits=len(hits), n_all=n_all,
                run_scope=((cur or {}).get("params") or {}).get("hk_scope"), HK_SCOPE_LABEL=HK_SCOPE_LABEL, in_connect=in_connect,
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
    if form.get("hk_scope") in HK_SCOPE_LABEL:
        flags.append(f"--hk-scope={form.get('hk_scope')}")
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
