"""命令行入口：stock <命令>。"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import requests

from . import app as appmod
from . import (brief, consensus, coverage, docs, dossier, evaluate, imafeed, jobs, monitor, newco, notify, profile, quotes, scenario_watch,
               tracker, triage, twitterfeed)
from .app import SOURCE_LABEL, App, backfill_releases, refine_news_dates
from .config import ConfigError
from .llm import LLMError
from .utils import fmt_local, iso, lock_holder, pct, today_local, utcnow
from .valuation import TARGET_LABEL, describe, fmt

LLM_TASKS = ("triage", "stock_brief", "market_brief", "profile", "research", "dossier", "review", "monitor", "tracker", "chat",
             "screen", "screen_theme")


def _print(msgs: list[str]) -> None:
    for m in msgs:
        print("  " + m)


def _load(args) -> App:
    try:
        return App.load(args.config)
    except ConfigError as e:
        sys.exit(str(e))


# ---------------------------------------------------------------- 命令

def cmd_init(args) -> None:
    root = Path(args.config).resolve().parent
    env_file, example = root / ".env", root / ".env.example"
    if not env_file.exists() and example.exists():
        shutil.copy(example, env_file)
        print("已创建 .env（来自 .env.example），请在其中填写 API key")
    app = _load(args)
    inbox = app.data_dir / "inbox"
    for c in app.watchlist.companies:
        (inbox / c.id).mkdir(parents=True, exist_ok=True)
    for d in ("docs", "reports"):
        (app.data_dir / d).mkdir(parents=True, exist_ok=True)
    print(f"数据目录：{app.data_dir}")
    print(f"收件箱：{inbox}/<公司id>/（年报、研报等放这里，下次运行自动导入）")
    print("自选股：" + "、".join(c.title for c in app.watchlist.companies))
    reason = app.llm.unavailable_reason("triage")
    print(f"LLM：{app.llm.describe('triage')}" + (f"（尚不可用：{reason}）" if reason else ""))


def cmd_fetch(args) -> None:
    app = _load(args)
    companies = app.watchlist.select(args.company)
    print("抓取数据…")
    _print(appmod.fetch(app, companies, prices=not args.no_prices, filings=not args.no_filings, news=not args.no_news))


def cmd_import(args) -> None:
    app = _load(args)
    msgs = docs.import_inbox(app.db, app.data_dir, app.watchlist)
    _print(msgs or ["收件箱为空"])


def cmd_triage(args) -> None:
    app = _load(args)
    print(f"事件分级（{app.llm.describe('triage')}）…")
    _print(triage.run(app, app.watchlist.select(args.company)))


def cmd_profile(args) -> None:
    app = _load(args)
    print(f"生成公司画像（{app.llm.describe('profile')}）…")
    with ThreadPoolExecutor(max_workers=app.settings.llm.concurrency) as pool:
        _print(list(pool.map(lambda c: profile.generate(app, c, force=args.force), app.watchlist.select(args.company))))


def _open(path: Path) -> None:
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.run([opener, str(path)], check=False)


def _brief(app: App, companies, use_llm: bool, push: bool, open_: bool, tracked: list[str] | None = None) -> None:
    """tracked：标的追踪中值得推送的变化，并入日报推送（不单独再推一次）。"""
    print("生成简报…")
    md_path, html_path, summary, msgs = brief.build(app, companies, use_llm=use_llm)
    _print(msgs)
    print(f"  简报：{html_path}")
    if push:
        if tracked:
            summary = "目标价修正：\n" + "\n".join(f"- {t}" for t in tracked) + f"\n（{tracker.DISCLAIMER}）\n\n" + summary
        _print(notify.push(app.settings, f"【自选股日报】{today_local():%Y-%m-%d}", summary) or ["未配置推送渠道（见 config.yaml notify）"])
    if open_:
        _open(html_path)


def cmd_brief(args) -> None:
    app = _load(args)
    _brief(app, app.watchlist.select(args.company), not args.no_llm, args.push, args.open)


def _ensure_reports(app: App, companies) -> list[str]:
    """每天最多检查一次定期报告，缺失的自动下载。"""
    msgs = []
    today = today_local().isoformat()
    for c in companies:
        key = f"reports_checked:{c.id}"
        if app.db.kv_get(key) == today:
            continue
        try:
            msgs += docs.fetch_reports(app.db, app.session, app.data_dir, c, app.settings.sources.sec_user_agent)
            msgs += backfill_releases(app, c)
            app.db.kv_set(key, today)
        except Exception as e:
            msgs.append(f"✗ {c.name} 定期报告检查失败：{e}")
    return msgs


PIPELINE_BUSY = "上一次运行（stock run / alert / target run）尚未结束，本次跳过"


def cmd_run(args) -> None:
    app = _load(args)
    with app.lock("pipeline") as ok:
        if not ok:
            print(PIPELINE_BUSY)
            return
        _run(app, args)


def _run(app: App, args) -> None:
    companies = app.watchlist.select(args.company)
    t0 = time.monotonic()
    print("1/6 导入收件箱…")
    _print(docs.import_inbox(app.db, app.data_dir, app.watchlist) or ["收件箱为空"])
    print("2/6 抓取行情、公告、新闻…")
    _print(appmod.fetch(app, companies))
    _print(scenario_watch.check(app, tracker.tracked(app, companies), push=args.push))  # 收盘行情已入库：现价所处情景有没有变
    if app.settings.consensus.enabled:
        print("   业绩对比一致预期…")
        _print(consensus.snapshot(app, companies) + consensus.check(app, companies) or ["没有新发布的业绩"])
    if app.settings.sources.auto_download_reports:
        print("3/6 检查定期报告…")
        _print(_ensure_reports(app, companies) or ["定期报告齐全"])
    use_llm = not args.no_llm
    if use_llm and app.settings.report.auto_profile:
        todo = [c for c in companies if profile.needs_profile(app, c)]
        if todo:
            print(f"   生成公司画像（{len(todo)} 家，{app.llm.describe('profile')}）…")
            with ThreadPoolExecutor(max_workers=app.settings.llm.concurrency) as pool:
                _print(list(pool.map(lambda c: profile.generate(app, c), todo)))
    if use_llm:
        print(f"4/6 事件分级（{app.llm.describe('triage')}）…")
        _print(triage.run(app, companies))
        print("   检查证伪条件…")
        _print(monitor.run(app, companies) or ["无新触发"])
    else:
        print("4/6 跳过事件分级（--no-llm）")
        _print(monitor.run(app, companies))  # 价格类条件不需要 LLM
    print(f"5/6 标的追踪（{app.llm.describe('tracker') if use_llm else '--no-llm：只检查新报告与待处理依据'}）…")
    res = tracker.run(app, companies, origin="run", use_llm=use_llm, progress=print)
    _print(res.msgs)
    _print(scenario_watch.check(app, tracker.tracked(app, companies), push=args.push))  # 估值修正后情景归属可能变
    gaps = coverage.report(app, companies)
    if gaps:
        print("数据缺口汇总（完整列表：stock status）：")
        _print(gaps)
    print("6/6 ", end="")
    _brief(app, companies, use_llm, args.push, args.open, tracked=res.notable)
    print(f"完成，用时 {time.monotonic() - t0:.0f} 秒")


def cmd_quotes(args) -> None:
    app = _load(args)
    with app.lock("quotes") as ok:
        if not ok:
            print("已有行情刷新在运行，跳过")
            return
        _print(quotes.refresh(app, app.watchlist.select(args.company), push=not args.no_push) or ["追踪中公司所在的市场均已休市（或没有追踪中的公司），无需刷新"])


def cmd_alert(args) -> None:
    app = _load(args)
    with app.lock("pipeline") as ok:
        if not ok:
            print(PIPELINE_BUSY)
            return
        companies = app.watchlist.select(args.company)
        _print(appmod.fetch(app, companies, market=False, ima=app.settings.sources.ima.in_alert, twitter=False))
        _print(triage.run(app, companies))
        _print(monitor.run(app, companies))
        _print(brief.alerts(app, do_push=not args.no_push))


DOSSIER_BUSY = ("已有深度报告在生成（进程 {pid}）：同一时间只运行一个 stock dossier——4 个研究 agent 已经会占满免费搜索源和"
                "模型网关的账号池。等它结束后再运行")


def cmd_dossier(args) -> None:
    app = _load(args)
    if reason := app.llm.unavailable_reason("research"):
        sys.exit(f"LLM 不可用：{reason}")
    with app.lock("dossier") as ok:
        if not ok:
            sys.exit(DOSSIER_BUSY.format(pid=lock_holder(app.lock_path("dossier")) or "?"))
        _dossier(app, args)


def _dossier(app: App, args) -> None:
    print(f"生成深度报告（研究：{app.llm.describe('research')}；撰写：{app.llm.describe('dossier')}）")
    for c in app.watchlist.select(args.company):
        t0 = time.monotonic()
        try:
            r = dossier.generate(app, c, progress=print, reuse_notes=args.reuse_notes, refill=args.refill)
        except LLMError as e:
            print(f"  ✗ {c.name}：{e}")
            continue
        card = r["card"]
        print(f"  {c.name}：完成（{time.monotonic() - t0:.0f} 秒）· 证伪条件 {len(card['kill_criteria'])} 条 · "
              f"催化剂 {len(card['catalysts'])} 条 · 产业链标的 {len(card['related_tickers'])} 个 · 跟踪词 {len(card['news_keywords'])} 个")
        print(f"    {r['html_path']}" + (f"\n    {r['pdf_path']}" if r["pdf_path"] else ""))
        if r["fixes"]:
            print(f"    一致性核对：修正了 {len(r['fixes'])} 处与研究笔记矛盾的「未披露/未查到」说法")
        if r.get("added"):
            print(f"    覆盖检查：补写了 {len(r['added'])} 条原稿遗漏的关键事实")
        if r["requests"]:
            lines = [f"#{q['id']} {q['item']}：{q['ask']}" for q in r["requests"]]
            print("    需要你补充的数据（补齐后运行 stock dossier " + c.id + " --reuse-notes）：")
            for line in lines:
                print("      " + line)
            if notify.channels(app.settings):
                _print(notify.push(app.settings, f"【需要你补充的数据】{c.name} {len(lines)} 项", "\n".join(lines)))
        if (t := app.db.tracker(c.id)) and t["enabled"]:  # 追踪中的公司：用新报告重建估值基线
            try:
                rev = tracker.rebase(app, c, origin="dossier")
                print(f"    标的追踪：已用新报告重建估值基线，{TARGET_LABEL} {fmt(rev['target'])}"
                      + "".join(f"\n      ⚠ {f}" for f in rev["flags"] or []))
            except (tracker.TrackerError, ValueError) as e:
                print(f"    ✗ 标的追踪重建基线失败：{e}")
        if args.open:
            _open(Path(r["pdf_path"] or r["html_path"]))


def cmd_track(args) -> None:
    app = _load(args)
    companies = app.watchlist.select(args.company)
    if args.check:
        _print(monitor.run(app, companies) or ["无新触发"])
    for c in companies:
        d = app.db.latest_dossier(c.id)
        if not d:
            print(f"{c.title}：还没有深度报告（stock dossier {c.id}）")
            continue
        print(f"{c.title}　{d['created_at'][:10]} 深度报告：{d['card']['thesis']}")
        if line := tracker.status_line(app, c):
            print(f"  标的追踪：{line}")
        stance = monitor.current_stance(app, c)
        for t in app.db.triggers(c.id):
            d = t.get("direction") or "down"
            role = "证伪" if dossier.falsifies(stance, d) else "印证"
            print(f"  [{brief.STATUS_LABEL[t['status']]}·{dossier.DIRECTION_LABEL[d]}·{role}] {t['description']}　→ {dossier.ACTION_LABEL[t['action']]}"
                  + (f"\n      依据：{t['evidence']}" if t["evidence"] and t["status"] != "ok" else ""))
        for cat in app.db.catalysts(c.id):
            print(f"  催化剂 {cat['when_text']}：{cat['event']}（KPI：{cat['kpi'] or '—'}）")
        plan = app.db.plan(c.id) or {}
        if plan.get("related_tickers"):
            print("  产业链标的：" + "、".join(f"{r['name']}({r['code']})" for r in plan["related_tickers"]))
        if plan.get("news_keywords"):
            print("  新闻跟踪词：" + "、".join(plan["news_keywords"]))


def cmd_target(args) -> None:
    app = _load(args)
    sub = args.target_cmd
    if sub == "list":
        _target_list(app, app.watchlist.select(args.company))
        return
    companies = app.watchlist.select(args.company)
    if sub == "add":
        for c in companies:
            if app.db.latest_dossier(c.id) is None and args.dossier:
                print(f"{c.name}：还没有深度报告，先生成…")
                with app.lock("dossier") as ok:
                    if not ok:
                        print("  " + DOSSIER_BUSY.format(pid=lock_holder(app.lock_path("dossier")) or "?"))
                        continue
                    try:
                        dossier.generate(app, c, progress=print)
                    except LLMError as e:
                        print(f"  ✗ {c.name}：{e}")
                        continue
            try:
                print(tracker.add(app, c, origin=tracker._origin("cli")))
            except (tracker.TrackerError, ValueError) as e:
                print(f"✗ {e}")
    elif sub == "remove":
        for c in companies:
            print(tracker.remove(app, c))
    elif sub == "rebase":
        for c in companies:
            try:
                rev = tracker.rebase(app, c, origin=tracker._origin("cli"))
                print(f"{c.name}：已用深度报告 #{rev['dossier_id']} 重建估值基线，{TARGET_LABEL} {fmt(rev['target'])}"
                      + "".join(f"\n    ⚠ {f}" for f in rev["flags"] or []))
            except (tracker.TrackerError, ValueError) as e:
                print(f"✗ {e}")
    elif sub == "show":
        for c in companies:
            _target_show(app, c, args.limit)
    elif sub == "run":
        with app.lock("pipeline") as ok:
            if not ok:
                print(PIPELINE_BUSY)
                return
            targets = tracker.tracked(app, companies)
            if not targets:
                print("没有追踪中的公司（stock target add <公司>）")
                return
            if not args.no_fetch:
                print("抓取与分级…")
                _print(appmod.fetch(app, targets))
                _print(triage.run(app, targets))
                _print(monitor.run(app, targets) or ["证伪条件无新触发"])
            print(f"标的追踪（{app.llm.describe('tracker')}）…")
            res = tracker.run(app, targets, origin="cli", progress=print)
            _print(res.msgs)
            _print(scenario_watch.check(app, targets, push=args.push))
            if args.push and res.notable:
                _print(notify.push(app.settings, f"【目标价修正】{today_local():%Y-%m-%d}",
                                   "\n".join(res.notable) + f"\n（{tracker.DISCLAIMER}）") or ["未配置推送渠道（见 config.yaml notify）"])


def _target_list(app: App, companies) -> None:
    for c in companies:
        t = app.db.tracker(c.id)
        if not t:
            print(f"{c.title}：未追踪" + ("" if app.db.latest_dossier(c.id) else "（还没有深度报告）"))
            continue
        line = tracker.status_line(app, c)
        print(f"{c.title}：{'追踪中' if t['enabled'] else '已停止'}" + (f"\n  {line}" if line else "")
              + (f"\n  上次检查 {fmt_local(t['checked_at'])}：{t['last_msg']}" if t["checked_at"] else ""))


def _target_show(app: App, c, limit: int) -> None:
    s = tracker.status(app, c)
    if not s:
        print(f"{c.title}：未追踪（stock target add {c.id}）")
        return
    print(f"{c.title}　{tracker.status_line(app, c)}")
    print(describe(s["live_model"], app.settings.tracker.target, s["price"], s["revision"].get("stance"),
                   app.settings.tracker.stance_band))
    if s["fx_note"]:
        print(f"（按最新汇率重算：{s['fx_note']}）")
    print(f"\n修正记录（最近 {limit} 条）：")
    for r in app.db.revisions(c.id, limit=limit):
        head = (f"#{r['id']} {fmt_local(r['created_at'])} {tracker.KIND_LABEL[r['kind']]}｜{TARGET_LABEL} {fmt(r['prev_target'])} → "
                f"{fmt(r['target'])}" if r["prev_target"] else f"#{r['id']} {fmt_local(r['created_at'])} "
                f"{tracker.KIND_LABEL[r['kind']]}｜{TARGET_LABEL} {fmt(r['target'])}")
        print(f"  {head}｜{r['summary']}")
        for ch in r["changes"] or []:
            print(f"    · {ch['label']}：{fmt(ch['old'])} → {fmt(ch['new'])}（{ch['reason']}）"
                  + (f"〔依据 {'、'.join(ch['evidence'])}〕" if ch["evidence"] else ""))
        for rj in r["rejected"] or []:
            print(f"    ✗ 未采纳：{rj['why']}")
        for f in r["flags"] or []:
            print(f"    ⚠ {f}")
    print(f"\n{tracker.DISCLAIMER}")


def cmd_consensus(args) -> None:
    app = _load(args)
    sub = args.consensus_cmd
    if sub == "set":
        c = app.watchlist.select([args.company])[0]
        given = [(m, v) for m, v in (("eps", args.eps), ("profit", args.profit), ("revenue", args.revenue)) if v is not None]
        if not given:
            sys.exit("请用 --eps、--profit 或 --revenue 给出一致预期（归母净利与营收单位为亿）")
        for metric, value in given:
            try:
                print(consensus.save_manual(app, c, args.year, metric, value, args.currency, args.date, args.n, args.source,
                                            args.high if len(given) == 1 else None, args.low if len(given) == 1 else None))
            except ValueError as e:
                sys.exit(f"✗ {e}")
        return
    companies = app.watchlist.select(args.company)
    if sub == "check":
        _print(consensus.snapshot(app, companies) + consensus.check(app, companies) or ["没有新发布的业绩"])
        return
    for c in companies:
        print(consensus.report_text(app, c, n=args.limit))
        print()


SCREEN_BUSY = "选股器已在运行（另一个 stock screen 进程持有锁，PID {pid}）"


def cmd_screen(args) -> None:
    from .screen import evaluate as sc_eval
    from .screen import promote as sc_promote
    from .screen import run as sc_run
    from .screen import store as sc_store
    from .screen import themes as sc_themes
    from .screen import universe as sc_universe

    app = _load(args)
    sub = args.screen_cmd
    markets = [args.market] if getattr(args, "market", None) else None
    if sub in ("refresh", "run") or (sub == "themes" and args.suggest is not None):
        with app.lock("screen") as ok:
            if not ok:
                sys.exit(SCREEN_BUSY.format(pid=lock_holder(app.lock_path("screen")) or "?"))
            if sub == "refresh":
                _print(sc_universe.refresh(app, markets, force=args.force, progress=print))
                from .screen import calendar as sc_calendar
                from .screen import events as sc_events
                _print(sc_events.scan(app, markets, progress=print))
                _print(sc_calendar.refresh(app, markets, force=args.force, progress=print))
            elif sub == "run":
                pools = ("long",) if args.long_only else ("event",) if args.event_only else ("long", "event")
                if not args.no_llm and (reason := app.llm.unavailable_reason("screen")):
                    print(f"模型不可用（{reason}），只做代码筛选")
                    args.no_llm = True
                res = sc_run.run(app, pools, markets, use_llm=not args.no_llm, refresh=not args.no_refresh, progress=print)
                _print(res.msgs)
                for pool, rid in res.runs.items():
                    _screen_show(app, rid, tiers=("A", "B", "C"), limit=args.limit)
                if args.push:
                    _screen_push(app, res.runs)
            else:
                _print(sc_themes.suggest(app, args.suggest or None, progress=print))
        return
    if sub == "show":
        pools = [args.pool] if args.pool else ["long", "event"]
        for pool in pools:
            r = sc_store.runs(app.db, pool, 1) if args.run is None else [x for x in sc_store.runs(app.db, None, 200) if x["id"] == args.run]
            if not r:
                print(f"{pool}：还没有运行记录（stock screen run）")
                continue
            _screen_show(app, r[0]["id"], tiers=None if args.all else tuple(args.tier.split(",")), limit=args.limit)
    elif sub == "promote":
        for key in args.keys:
            try:
                cid, msg = sc_promote.promote(app, key)
            except sc_promote.PromoteError as e:
                print(f"✗ {e}")
                continue
            print(msg)
            print(f"  下一步：stock fetch {cid} && stock docs fetch {cid} && stock triage {cid} && stock profile {cid} "
                  f"&& stock dossier {cid} && stock target add {cid}（或在 WebUI 提交「新公司初始化」）")
    elif sub == "eval":
        rows = sc_eval.collect(app, days=args.days)
        print("选股器事后检验：入选当日收盘起算的超额收益（港股对恒生指数、A 股对沪深 300）；样本少时结论不可靠。")
        print(sc_eval.summarize(rows))
    elif sub == "themes":
        if args.accept:
            print(sc_themes.accept(app, args.accept))
            return
        if args.dismiss:
            print("已忽略" if sc_store.set_theme_idea(app.db, args.dismiss, "dismissed") else "没有这条建议")
            return
        t = sc_themes.load(app)
        mem, missing = sc_themes.memberships(app, t)
        print(f"主题文件：{sc_themes.path(app)}（{len(t.themes)} 个主题）")
        names = {u["secid"]: f"{u['name']}({u['market']}:{u['code']})" for u in sc_store.universe(app.db)}
        for th in t.themes:
            print(f"- {th.name}（{th.id}）")
            for n in th.nodes:
                ok = [names[s] for s, ms in mem.items() for m in ms if m["theme_id"] == th.id and m["node"] == n.name]
                print(f"    {n.name}{'［瓶颈］' if n.bottleneck else ''}：{'、'.join(ok) or '（无股票池内标的）'}")
        if missing:
            print("不在股票池的代码：" + "；".join(missing))
        ideas = sc_store.theme_ideas(app.db)
        if ideas:
            print("主题 agent 的待处理建议（stock screen themes --accept ID / --dismiss ID）：")
            for i in ideas:
                print(f"  #{i['id']} {i['theme']} / {i['node']}：{i['name']}（{i['market']}:{i['code']}）{i['reason'][:120]}")


def _screen_line(h: dict) -> str:
    from .screen.buckets import BUCKET_LABEL
    from .screen.quick import VERDICT_LABEL

    m = h["metrics"]
    fm = m.get("floor") or {}
    parts = [f"{h['tier']} {h['name']} {h['market']}:{h['code']}", f"现价 {h['price']:.4g} {h['currency']}",
             "[" + "、".join(BUCKET_LABEL.get(b, b) for b in h["buckets"].split(",")) + f"] 分 {h['score']:.0f}"]
    if fm:
        parts.append(f"保守 FCF Yield {pct(fm.get('yield_cons'), sign=False)} · 净现金/市值 {pct(fm.get('net_cash_ratio'), sign=False)}"
                     + (" · Negative EV" if fm.get("negative_ev") else ""))
    div = m.get("dividend") or {}
    if fm.get("dividend_yield") or div.get("years"):
        parts.append(f"股息率 {pct(fm.get('dividend_yield'), sign=False)}（连续派息 {div.get('years') or 0} 年）")
    if h.get("odds") is not None:
        parts.append(f"粗算赔率 {h['odds']:.1f}（上行 {pct(h['upside'])} / 下行 {pct(-h['downside'])}）")
    rev = m.get("revision") or {}
    if "revision" in h["buckets"]:
        parts.append(f"净利同比 {rev.get('profit_yoy') or 0:.0f}% · 收入同比 {rev.get('revenue_yoy') or 0:.0f}%")
    liq = m.get("liquidity")
    if liq:
        parts.append(f"20 日成交额中位数 {liq['median'] / 1e8:.2f} 亿")
    line = " · ".join(parts)
    if m.get("catalysts"):
        line += "\n      ✦ 催化剂：" + "；".join(m["catalysts"][:4])
    risks = [f["text"] for f in h["flags"] if f["severity"] in ("high", "mid")]
    if risks:
        line += "\n      ⚠ " + "；".join(risks[:3])
    notes = [f["text"] for f in h["flags"] if f["severity"] == "info"]
    if notes:
        line += "\n      ℹ " + "；".join(notes[:2])
    if h.get("quick"):
        q = h["quick"]
        line += f"\n      快筛：{VERDICT_LABEL.get(q['verdict'], q['verdict'])} — {q['summary']}"
    return line


def _screen_show(app: App, run_id: int, tiers: tuple[str, ...] | None = ("A", "B"), limit: int = 30) -> None:
    from .screen import store as sc_store
    from .screen.buckets import POOL_LABEL

    run = next(r for r in sc_store.runs(app.db, None, 500) if r["id"] == run_id)
    hits = [h for h in sc_store.hits(app.db, run_id) if tiers is None or h["tier"] in tiers]
    print(f"\n{POOL_LABEL[run['pool']]} · 运行 #{run_id}（{fmt_local(run['started_at'])}）· 显示 {min(len(hits), limit)}/{len(hits)} 家"
          + ("" if tiers is None else f"（{'、'.join(tiers)} 类）"))
    for h in hits[:limit]:
        print("  " + _screen_line(h))
    print(f"  （粗算赔率只用于排序：合理价值 = 保守 FCF ÷ {app.settings.screen.fair_fcf_yield:.0%} + 净现金；研究候选，不是买卖建议）")


def _screen_push(app: App, runs: dict[str, int]) -> None:
    from .screen import store as sc_store
    from .screen.buckets import POOL_LABEL

    lines = []
    for pool, rid in runs.items():
        top = [h for h in sc_store.hits(app.db, rid) if h["tier"] in ("A", "B")][:8]
        if top:
            lines.append(f"{POOL_LABEL[pool]}：" + "；".join(f"{h['tier']} {h['name']}({h['code']})" for h in top))
    if lines:
        _print(notify.push(app.settings, f"【选股器 A / B 类候选】{today_local():%Y-%m-%d}",
                           "\n".join(lines) + f"\n（{tracker.DISCLAIMER}）") or ["未配置推送渠道"])


FUND_BUSY = "已有模拟基金任务在运行（进程 {pid}），等它结束后再运行"


def cmd_fund(args) -> None:
    """模拟基金（模拟组合信号，非投资建议）：sync 同步定义 / refresh 晚间刷新日线 / plan 盘前计划 / alert 盘中提醒 /
    close 盘后结算 / weekly 周报 / status 查看 / resume 恢复熔断暂停的基金。--dry-run 不写库、不推送。"""
    from datetime import datetime

    from .fund import config as fcfg
    from .fund import engine, report, store
    from .utils import CST

    app = _load(args)
    try:
        cfg = fcfg.load(app)
    except fcfg.FundsError as e:
        sys.exit(str(e))
    sub = args.fund_cmd
    day = date.fromisoformat(args.date) if getattr(args, "date", None) else today_local()
    dry = getattr(args, "dry_run", False)

    def send(msgs: list[tuple[str, str]]) -> None:
        for title, text in msgs:
            print(f"\n{title}\n{text}")
        if getattr(args, "push", False):
            if dry:
                print("\n（--dry-run：以上为将推送的内容，没有发送）")
                return
            for title, text in msgs:
                _print(notify.push(app.settings, title, text) or ["未配置推送渠道（见 config.yaml notify）"])

    if sub == "status":
        store.ensure(app.db)
        print(report.status_text(app, engine.fund_defs(app, cfg)))
        return
    with app.lock("fund") as ok:
        if not ok:
            sys.exit(FUND_BUSY.format(pid=lock_holder(app.lock_path("fund")) or "?"))
        if sub == "sync":
            _print(store.sync(app.db, cfg.funds, day.isoformat(), dry_run=dry))
            if dry:
                print("（--dry-run：没有写入）")
        elif sub == "refresh":
            _print(engine.refresh(app, cfg, [args.market] if args.market else None, limit=args.limit, dry_run=dry, progress=print))
        elif sub == "plan":
            plans, msgs = engine.plan(app, cfg, day, args.fund or None, dry_run=dry)
            if not plans:
                _print(msgs or ["今日没有需要出计划的基金"])
                return
            send(report.plan_messages(plans, day, msgs))
        elif sub == "alert":
            now = datetime.now(CST)
            alerts, msgs = engine.alert(app, cfg, now, dry_run=dry)
            if not alerts:
                _print(msgs + ["没有新的触发"])
                return
            send(report.alert_messages(alerts, now))
        elif sub == "close":
            results, msgs = engine.close(app, cfg, day, args.fund or None, dry_run=dry)
            if not results:
                _print(msgs or ["今日没有需要结算的基金"])
                return
            send(report.close_messages(app, results, day, msgs))
        elif sub == "weekly":
            send(report.weekly_messages(app, engine.fund_defs(app, cfg), day))
        elif sub == "resume":
            f = store.fund(app.db, args.fund_id)
            if f is None:
                sys.exit(f"没有基金 {args.fund_id}")
            if dry:
                print(f"（--dry-run）将把 {f['name']} 的熔断从「{f['breaker']}」恢复为 0")
                return
            store.resume(app.db, f["id"])
            print(f"{f['name']} 已恢复：熔断清零，下一次盘前计划起恢复开新仓（回撤仍按历史最高净值计算）")


def cmd_web(args) -> None:
    from .web.server import serve  # noqa: PLC0415 - 只有 stock web 需要 FastAPI

    try:
        serve(args.config, port=args.port, background=not args.no_scheduler, open_browser=args.open, scheduler_on=not args.jobs_only)
    except ConfigError as e:
        sys.exit(str(e))


def cmd_requests(args) -> None:
    app = _load(args)
    for rid, status in ((args.done, "done"), (args.dismiss, "dismissed")):
        if rid:
            print(f"#{rid}：" + ("已关闭" if app.db.close_request(rid, status) else "没有这个未关闭的请求"))
    companies = app.watchlist.select(args.company)
    rows = [r for c in companies for r in app.db.requests(c.id)]
    if not rows:
        print("没有待补充的数据请求")
        return
    names = {c.id: c.name for c in companies}
    for r in rows:
        print(f"#{r['id']} {names.get(r['company_id'], r['company_id'])}｜{r['item']}\n    为什么关键：{r['why']}\n    怎么补充：{r['ask']}")
    print("补充后运行 stock dossier <公司> --reuse-notes 重新撰写；用 stock requests --done 编号 关闭请求。")


def cmd_docs(args) -> None:
    app = _load(args)
    if args.docs_cmd == "fetch":
        for c in app.watchlist.select(args.company):
            _print((docs.fetch_reports(app.db, app.session, app.data_dir, c, app.settings.sources.sec_user_agent) + backfill_releases(app, c))
                   or [f"{c.name}：定期报告已是最新"])
    elif args.docs_cmd == "add":
        c = app.watchlist.get(args.company)
        if c is None:
            sys.exit(f"自选股中找不到：{args.company}")
        src = Path(args.file).expanduser()
        if not src.exists():
            sys.exit(f"文件不存在：{src}")
        doc_type = args.type or docs.infer_doc_type(src.name)
        doc, new = docs.store_document(
            app.db, app.data_dir, c.id, src, doc_type=doc_type,
            period=args.period or docs.infer_period(src.name, doc_type), title=args.title or src.stem, source="manual",
        )
        print(f"{'已导入' if new else '已存在'}：{c.name} · {docs.DOC_TYPES[doc['doc_type']]} {doc['period'] or ''} · "
              f"{doc['title']}（{doc['pages'] or '-'} 页，{doc['chars']:,} 字）")
    else:
        companies = app.watchlist.select(args.company)
        for c in companies:
            rows = app.db.documents(c.id)
            print(f"{c.title}：{len(rows)} 份")
            for d in rows:
                print(f"  #{d['id']} {docs.DOC_TYPES.get(d['doc_type'], d['doc_type'])} {d['period'] or ''} · {d['title']} · "
                      f"{d['source']} · {d['chars']:,} 字")


def cmd_data(args) -> None:
    app = _load(args)
    if args.data_cmd == "add":
        cid = ""
        if args.company != "-":
            c = app.watchlist.get(args.company)
            if c is None:
                sys.exit(f"自选股中找不到：{args.company}（行业/宏观指标用 - 代替公司）")
            cid = c.id
        app.db.upsert_manual(cid, args.metric, args.period, float(args.value.replace(",", "")), args.unit, args.note, "manual:cli")
        print(f"已保存：{args.metric} {args.period} = {args.value}{args.unit or ''}")
    elif args.data_cmd == "import":
        c = app.watchlist.get(args.company) if args.company else None
        _print([docs.import_manual_csv(app.db, Path(args.file).expanduser(), c.id if c else None, app.watchlist)])
    else:
        keys = [""] + [c.id for c in app.watchlist.select(args.company)]
        for cid in keys:
            rows = app.db.manual_data(cid)
            if rows:
                print(app.watchlist.get(cid).name if cid else "行业/宏观")
                for m in rows:
                    print(f"  {m['metric']} · {m['period']} = {m['value']}{m['unit'] or ''}  {m['note'] or ''}")


def cmd_note(args) -> None:
    app = _load(args)
    c = app.watchlist.get(args.company)
    if c is None:
        sys.exit(f"自选股中找不到：{args.company}")
    print(appmod.add_manual_item(app, c, args.title, args.detail or "", args.url))
    print("下次运行 stock triage / stock run 时会与新闻一起分级。")


def cmd_events(args) -> None:
    app = _load(args)
    since = iso(utcnow() - timedelta(days=args.days))
    for c in app.watchlist.select(args.company):
        rows = app.db.items_since(c.id, since, min_materiality=args.min)
        print(f"{c.title}：{len(rows)} 条（{args.days} 天内，重要性≥{args.min}）")
        for e in rows:
            print(f"  {fmt_local(e['published_at'])} ★{e['materiality']} {triage.DIRECTIONS.get(e['direction'], '')} "
                  f"[{triage.event_label(e)}] {e['summary_zh'] or e['title']}（{SOURCE_LABEL.get(e['source'], e['source'])}）")


def cmd_news_dates(args) -> None:
    app = _load(args)
    for line in refine_news_dates(app, args.days, args.limit):
        print(line)


def cmd_eval(args) -> None:
    app = _load(args)
    rows = evaluate.collect(app, app.watchlist.select(args.company), days=args.days)
    print(f"近 {args.days} 天已分级的利好/利空事件 {len(rows)} 条。可交易口径：从消息发布后第一个能成交的收盘价起算，"
          "超额收益相对各上市地的对比基准；样本少时结论不可靠。")
    print(evaluate.summarize(rows))


def cmd_llm_test(args) -> None:
    app = _load(args)
    names = [args.profile] if args.profile else list(app.settings.llm.profiles)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}, "answer": {"type": "string"}},
              "required": ["ok", "answer"], "additionalProperties": False}
    for name in names:
        prof = app.settings.llm.profiles[name]
        print(f"[{name}] {prof.protocol} · {prof.model} · {prof.base_url or '默认地址'}")
        try:
            t0 = time.monotonic()
            r = app.llm.complete("llm_test", ["你是测试助手。"], "用一句话说明 1+1 等于几。", profile=name, max_tokens=2000)
            print(f"  文本：{r.text.strip()[:80]}（{time.monotonic() - t0:.1f}s，served by {r.model}）")
            t0 = time.monotonic()
            r = app.llm.complete("llm_test", ["你是测试助手。"], "1+1 等于几？ok 填 true。", profile=name, schema=schema, max_tokens=2000)
            print(f"  JSON：{r.data}（{time.monotonic() - t0:.1f}s）")
        except LLMError as e:
            print(f"  ✗ {e}")
    print("任务路由：" + "；".join(f"{t} → {app.llm.describe(t)}" for t in LLM_TASKS))


def cmd_add(args) -> None:
    app = _load(args)
    try:
        if len(args.target) == 2:
            market, code = args.target
        elif len(args.target) == 1:
            market, code, name = newco.resolve(app, args.target[0])
            print(f"「{args.target[0]}」→ {name}（{market} {code}）")
        else:
            raise ValueError("用法：stock add 名称或代码，或 stock add 市场 代码")
        data = newco.build(app, market, code)
    except (ValueError, requests.RequestException) as e:
        sys.exit(f"新增失败：{e}")
    print("查到：" + newco.describe(data))
    if args.dry_run:
        return
    newco.save(app, data)
    print(f"已加入观察清单：{data['name']}（id {data['id']}）")
    if args.onboard:
        steps = jobs.TASKS["onboard"].steps
        for i, step in enumerate(steps, 1):
            print(f"\n== 初始化 {i}/{len(steps)}：stock {' '.join(step)} {data['id']}", flush=True)
            main(["-c", args.config, *(["-v"] if args.verbose else []), *step, data["id"]])


def cmd_ima(args) -> None:
    app = _load(args)
    from .sources import ima

    if ima.credentials() is None:
        sys.exit(f"未配置 ima 凭证：在 .env 填写 {ima.ENV_CLIENT_ID} 与 {ima.ENV_API_KEY}（https://ima.qq.com/agent-interface 获取）")
    try:
        if args.action == "list":
            wanted = set(app.settings.sources.ima.knowledge_bases)
            for kb in ima.knowledge_bases(app.session):
                mark = "✓" if wanted & {kb["id"], kb["name"]} else " "
                print(f"  {mark} {kb['name']}｜{kb['type']}｜{kb['role']}｜{kb['count']:,} 条")
            print("  （✓ 为 config.yaml sources.ima.knowledge_bases 中已配置的知识库）")
        elif args.action == "search":
            if not args.query:
                sys.exit("用法：stock ima search 关键词（只匹配标题）")
            for kb in imafeed.knowledge_bases(app):
                hits = [x for x in ima.search(app.session, kb["id"], args.query) if x["media_type"] != ima.FOLDER]
                hits.sort(key=lambda x: ima.parse_date(x["title"]) or date.min, reverse=True)
                print(f"「{kb['name']}」{len(hits)} 条：")
                for x in hits[: args.limit]:
                    print(f"  {ima.parse_date(x['title']) or '日期不详'}｜{ima.MEDIA_TYPES.get(x['media_type'], x['media_type'])}｜{x['title']}")
        else:
            if not imafeed.enabled(app):
                sys.exit("ima 未启用：检查 config.yaml sources.ima（enabled、knowledge_bases）")
            _print(imafeed.fetch(app, app.watchlist.select(args.query and [args.query])))
    except ima.ImaError as e:
        sys.exit(str(e))


def cmd_twitter(args) -> None:
    app = _load(args)
    from .sources import twitter

    if not twitter.available():
        sys.exit(f"推特不可用：需要安装 twitter-cli（pipx install twitter-cli），并在 .env 填写 {twitter.ENV_AUTH} 与 {twitter.ENV_CT0}"
                 "（或用 agent-reach configure twitter-cookies 保存 Cookie）")
    try:
        if args.action == "candidates":
            rows = twitterfeed.candidates(app, args.limit)
            if not rows:
                print("暂无候选账号（新闻源每次运行会记下检索到的帖子的作者；同一账号出现多次才会列出）")
            for r in rows:
                print(f"@{r['handle']}{'✓' if r['verified'] else ''}｜出现 {r['posts']} 次｜总赞 {r['likes']}｜{'、'.join(r['companies'].values())}\n    {r['sample']}\n    {r['url']}")
            print("加入账号：WebUI 设置 → 通用 → 推特 / X → 账号发现，或编辑 config.yaml 的 sources.twitter.shared_accounts / watchlist 的 twitter_official、twitter_accounts")
            return
        if args.action == "fetch":
            if not twitterfeed.enabled(app):
                sys.exit("推特新闻源未开启：在 WebUI 设置 → 通用 →「推特 / X」打开「新闻源」，或把 config.yaml 的 sources.twitter.news_feed 设为 true")
            _print(twitterfeed.fetch(app, app.watchlist.select(args.query and [args.query])))
            return
        if not args.query:
            sys.exit("用法：stock twitter search 检索词 / stock twitter user 账号名")
        posts = twitter.search(args.query, args.limit, app.settings.sources.twitter.days, app.settings.sources.twitter.min_likes) \
            if args.action == "search" else twitter.user_posts(args.query, args.limit)
    except twitter.TwitterError as e:
        sys.exit(str(e))
    for p in posts:
        print(f"{fmt_local(iso(p['at']))}｜@{p['author']}{'✓' if p['verified'] else ''}｜赞{p['likes']}｜{twitterfeed._text(p)[:120]}\n    {p['url']}")


def cmd_status(args) -> None:
    app = _load(args)
    s = app.db.stats()
    print(f"数据库：{app.db.path}")
    print(f"信息条目：{s['items']['n']}（已分级 {s['items']['triaged'] or 0}）")
    for r in s["items_by_source"]:
        print(f"  {SOURCE_LABEL.get(r['source'], r['source'])}：{r['n']} 条，最新 {fmt_local(r['latest'])}")
    print(f"行情序列：{len(s['bars'])} 个；文档：{s['documents']['n']}；手动数据：{s['manual']['n']}；公司画像：{len(s['profiles'])}")
    for t in LLM_TASKS:
        reason = app.llm.unavailable_reason(t)
        print(f"LLM {t}：{app.llm.describe(t)}" + (f"（不可用：{reason}）" if reason else ""))
    gaps = coverage.report(app, app.watchlist.companies)
    print("数据缺口：" + ("无" if not gaps else ""))
    _print(gaps)
    chans = notify.channels(app.settings)
    print("推送渠道：" + ("、".join(chans) if chans else "未配置"))
    kbs = app.settings.sources.ima.knowledge_bases
    print("ima 知识库：" + ("、".join(kbs) if imafeed.enabled(app) else "未启用（需要 .env 的 IMA_CLIENT_ID / IMA_API_KEY 与 sources.ima.knowledge_bases）"))
    from .sources import twitter

    tw = app.settings.sources.twitter
    ready = twitter.available()
    print("推特：" + (f"agent 工具{'开' if tw.agent_tools else '关'}、新闻源{'开' if tw.news_feed else '关'}" if ready
                    else "未启用（需要安装 twitter-cli 并配置 TWITTER_AUTH_TOKEN / TWITTER_CT0）"))


# ---------------------------------------------------------------- 解析

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stock", description="自选股研究系统：公告/新闻监控、LLM 事件分级、技术面快照、每日简报")
    p.add_argument("-c", "--config", default="config.yaml", help="配置文件路径（默认 ./config.yaml）")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_company(sp, nargs="*"):
        sp.add_argument("company", nargs=nargs, help="公司 id / 名称 / 代码，不填表示全部")
        return sp

    sub.add_parser("init", help="初始化配置、数据目录和收件箱").set_defaults(func=cmd_init)

    sp = with_company(sub.add_parser("run", help="完整流程：导入→抓取→定期报告→画像→分级→简报"))
    sp.add_argument("--push", action="store_true", help="推送简报摘要")
    sp.add_argument("--open", action="store_true", help="生成后在浏览器打开")
    sp.add_argument("--no-llm", action="store_true", help="不调用 LLM，只做数据")
    sp.set_defaults(func=cmd_run)

    sp = with_company(sub.add_parser("fetch", help="抓取行情、公告、新闻"))
    sp.add_argument("--no-prices", action="store_true")
    sp.add_argument("--no-filings", action="store_true")
    sp.add_argument("--no-news", action="store_true")
    sp.set_defaults(func=cmd_fetch)

    sub.add_parser("import", help="导入 data/inbox 中的文件").set_defaults(func=cmd_import)
    with_company(sub.add_parser("triage", help="对未分级的信息做 LLM 分级")).set_defaults(func=cmd_triage)

    sp = with_company(sub.add_parser("profile", help="根据年报等资料生成公司画像"))
    sp.add_argument("--force", action="store_true", help="资料未变化也重新生成")
    sp.set_defaults(func=cmd_profile)

    sp = with_company(sub.add_parser("brief", help="生成每日简报"))
    sp.add_argument("--push", action="store_true")
    sp.add_argument("--open", action="store_true")
    sp.add_argument("--no-llm", action="store_true")
    sp.set_defaults(func=cmd_brief)

    qp = sub.add_parser("quotes", help="行情刷新（无模型）：追踪中公司各上市地的实时快照，休市的市场跳过；刷新后检查情景切换")
    qsub = qp.add_subparsers(dest="quotes_cmd", required=True)
    sp = with_company(qsub.add_parser("refresh", help="取快照（价格、昨收、涨跌幅），不拉日线；价格进入新的情景时页面通知并推送"))
    sp.add_argument("--no-push", action="store_true", help="情景切换只写库与页面，不推送")
    sp.set_defaults(func=cmd_quotes)

    sp = with_company(sub.add_parser("alert", help="盘中：抓公告和新闻→分级→推送重要事件"))
    sp.add_argument("--no-push", action="store_true")
    sp.set_defaults(func=cmd_alert)

    sp = with_company(sub.add_parser("dossier", help="生成深度报告（带工具的研究 agent；含证伪条件、催化剂、跟踪计划）"))
    sp.add_argument("--open", action="store_true", help="生成后打开")
    sp.add_argument("--reuse-notes", action="store_true", help="复用 3 天内保存的研究笔记，跳过研究直接撰写")
    sp.add_argument("--refill", action="store_true", help="与 --reuse-notes 一起用：强制重新补缺（例如配置了付费搜索 API 之后）")
    sp.set_defaults(func=cmd_dossier)

    sp = with_company(sub.add_parser("track", help="查看证伪条件、催化剂与跟踪计划"))
    sp.add_argument("--check", action="store_true", help="先检查一遍证伪条件")
    sp.set_defaults(func=cmd_track)

    tp = sub.add_parser("target", help="标的追踪：按新事件修正估值假设，目标价（情景加权估值）由代码重算")
    tsub = tp.add_subparsers(dest="target_cmd", required=True)
    with_company(tsub.add_parser("list", help="追踪状态与目标价"))
    sp = with_company(tsub.add_parser("add", help="开始追踪（用最新深度报告建立估值基线）"), nargs="+")
    sp.add_argument("--dossier", action="store_true", help="还没有深度报告时先生成")
    with_company(tsub.add_parser("remove", help="停止追踪（历史修正记录保留）"), nargs="+")
    with_company(tsub.add_parser("rebase", help="用最新深度报告重建估值基线"), nargs="+")
    sp = with_company(tsub.add_parser("show", help="当前估值模型与修正记录"), nargs="+")
    sp.add_argument("--limit", type=int, default=10)
    sp = with_company(tsub.add_parser("run", help="抓取、分级后，按新依据修正估值（stock run 已包含这一步）"))
    sp.add_argument("--no-fetch", action="store_true", help="不抓取，只处理已分级的依据")
    sp.add_argument("--push", action="store_true", help="推送值得关注的修正")
    tp.set_defaults(func=cmd_target)

    cp = sub.add_parser("consensus", help="业绩 vs 一致预期：show 查看当前一致预期与历史对比 / set 录入（港股）/ check 检查新业绩")
    csub = cp.add_subparsers(dest="consensus_cmd", required=True)
    sp = with_company(csub.add_parser("show", help="当前一致预期、修正趋势与历史各期的业绩对比（联网）"))
    sp.add_argument("--limit", type=int, default=6, help="列出最近几个报告期")
    with_company(csub.add_parser("check", help="记录一致预期快照，检查新发布的业绩（stock run 已包含这一步）"))
    sp = csub.add_parser("set", help="录入一致预期（港股没有免费数据源），例：stock consensus set xpeng 2026 --eps -2.4 --currency CNY --n 20 --source 富途")
    sp.add_argument("company")
    sp.add_argument("year", type=int, help="财年，如 2026")
    sp.add_argument("--eps", type=float, help="每股收益")
    sp.add_argument("--profit", type=float, help="归母净利（亿）")
    sp.add_argument("--revenue", type=float, help="营业收入（亿）")
    sp.add_argument("--currency", required=True, choices=["CNY", "HKD", "USD"], help="数据来源的币种（内地公司的港股报表多为人民币）")
    sp.add_argument("--n", type=int, help="机构数")
    sp.add_argument("--high", type=float, help="最高预测（只录入一个指标时有效）")
    sp.add_argument("--low", type=float, help="最低预测（只录入一个指标时有效）")
    sp.add_argument("--source", help="来源，如 富途、彭博")
    sp.add_argument("--date", help="数据日期 YYYY-MM-DD（默认今天；补录业绩发布前的数据时填当时的日期）")
    cp.set_defaults(func=cmd_consensus)

    sp = with_company(sub.add_parser("requests", help="查看或关闭「需要你补充的数据」请求"))
    sp.add_argument("--done", type=int, metavar="编号", help="标记已补充")
    sp.add_argument("--dismiss", type=int, metavar="编号", help="忽略该请求")
    sp.set_defaults(func=cmd_requests)

    dp = sub.add_parser("docs", help="文档：fetch 自动下载定期报告 / add 手动添加 / list 查看")
    dsub = dp.add_subparsers(dest="docs_cmd", required=True)
    with_company(dsub.add_parser("fetch", help="下载最新年报和中期报告"))
    sp = dsub.add_parser("add", help="手动添加文档（PDF/DOCX/TXT/MD/HTML；图片用 tesseract 识别文字）")
    sp.add_argument("company")
    sp.add_argument("file")
    sp.add_argument("--type", choices=list(docs.DOC_TYPES), help="不填则按文件名推断")
    sp.add_argument("--period", help="如 2025、2026H1、2026Q3；不填则按文件名推断")
    sp.add_argument("--title")
    with_company(dsub.add_parser("list", help="查看已入库文档"))
    dp.set_defaults(func=cmd_docs)

    dp = sub.add_parser("data", help="手动数据：add 单条 / import CSV / list 查看")
    dsub = dp.add_subparsers(dest="data_cmd", required=True)
    sp = dsub.add_parser("add", help="例：stock data add dongyue R32价格 2026-09 45000 --unit 元/吨")
    sp.add_argument("company", help="公司；行业/宏观指标用 -")
    sp.add_argument("metric")
    sp.add_argument("period")
    sp.add_argument("value")
    sp.add_argument("--unit")
    sp.add_argument("--note")
    sp = dsub.add_parser("import", help="导入 CSV（列：metric,period,value,unit,note,source,company）")
    sp.add_argument("file")
    sp.add_argument("--company")
    with_company(dsub.add_parser("list"))
    dp.set_defaults(func=cmd_data)

    sp = sub.add_parser("note", help="手动录入一条消息（会参与分级）")
    sp.add_argument("company")
    sp.add_argument("title")
    sp.add_argument("--detail")
    sp.add_argument("--url")
    sp.set_defaults(func=cmd_note)

    sp = with_company(sub.add_parser("events", help="查看已分级事件"))
    sp.add_argument("--days", type=int, default=3)
    sp.add_argument("--min", type=int, default=3, help="最低重要性")
    sp.set_defaults(func=cmd_events)

    sp = with_company(sub.add_parser("eval", help="事后检验：事件分级的方向与重要性有没有预测力"))
    sp.add_argument("--days", type=int, default=90)
    sp.set_defaults(func=cmd_eval)

    sp = sub.add_parser("news-dates", help="回填：把已入库 Google 新闻的时间改为原文页声明的真实发布时间（联网）")
    sp.add_argument("--days", type=int, default=14, help="只处理最近多少天入库时间内的条目")
    sp.add_argument("--limit", type=int, default=200)
    sp.set_defaults(func=cmd_news_dates)

    sp = sub.add_parser("llm-test", help="测试 LLM 配置（地址、模型、JSON 输出）")
    sp.add_argument("--profile")
    sp.set_defaults(func=cmd_llm_test)

    sub.add_parser("status", help="查看数据与配置状态").set_defaults(func=cmd_status)

    sp = sub.add_parser("add", help="按名称或代码新增公司：自动查名称、英文名、行业、其他上市地（A+H、港股+美股），写入观察清单")
    sp.add_argument("target", nargs="+", help="名称或代码（腾讯控股、2577、300308、XPEV），或 市场 代码（HK 2577）")
    sp.add_argument("--onboard", action="store_true", help="接着初始化：抓取 → 下载年报 → 分级 → 画像 → 深度报告 → 开始追踪")
    sp.add_argument("--dry-run", action="store_true", help="只查询，不写入观察清单")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("ima", help="ima 知识库：list 可访问的知识库 / search 按标题检索 / fetch 只检索 ima 新条目")
    sp.add_argument("action", choices=["list", "search", "fetch"])
    sp.add_argument("query", nargs="?", help="search 的关键词；fetch 时可指定一家公司")
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(func=cmd_ima)

    sp = sub.add_parser("twitter", help="推特 / X：search 检索 / user 读取账号 / fetch 只抓取新闻源 / candidates 账号发现的候选账号")
    sp.add_argument("action", choices=["search", "user", "fetch", "candidates"])
    sp.add_argument("query", nargs="?", help="search 的检索词、user 的账号名；fetch 时可指定一家公司")
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(func=cmd_twitter)

    scp = sub.add_parser("screen", help="选股器：港股通与 A 股全市场初筛 → 代码打分 → 模型快筛；点选送深研（研究候选，不是买卖建议）")
    ssub = scp.add_subparsers(dest="screen_cmd", required=True)
    sp = ssub.add_parser("refresh", help="刷新股票池、报表数据与全市场公告事件")
    sp.add_argument("--market", choices=["HK", "A"])
    sp.add_argument("--force", action="store_true", help="忽略缓存，重新抓取报表")
    sp = ssub.add_parser("run", help="运行选股（默认先刷新数据；两个池：长线池与事件池）")
    sp.add_argument("--market", choices=["HK", "A"])
    sp.add_argument("--long-only", action="store_true", help="只跑长线池")
    sp.add_argument("--event-only", action="store_true", help="只跑事件池")
    sp.add_argument("--no-llm", action="store_true", help="不调用模型快筛，只用代码分层")
    sp.add_argument("--no-refresh", action="store_true", help="不刷新数据，用库里已有的股票池与事件")
    sp.add_argument("--push", action="store_true", help="推送 A、B 类候选")
    sp.add_argument("--limit", type=int, default=20)
    sp = ssub.add_parser("show", help="查看最近一次的候选")
    sp.add_argument("--pool", choices=["long", "event"])
    sp.add_argument("--tier", default="A,B,C", help="显示的分层，逗号分隔（默认 A,B,C；A 高胜率+高赔率、B 高胜率、C 高赔率、D 观察、X 快筛淘汰）")
    sp.add_argument("--all", action="store_true", help="显示全部分层（含快筛淘汰）")
    sp.add_argument("--run", type=int, help="运行编号")
    sp.add_argument("--limit", type=int, default=40)
    sp = ssub.add_parser("promote", help="送深研：把候选加入观察清单（之后运行新公司初始化）")
    sp.add_argument("keys", nargs="+", help="代码，如 00700、HK:00700、600519")
    sp = ssub.add_parser("eval", help="事后检验：候选入选后的超额收益与胜率（按池、分层、策略桶）")
    sp.add_argument("--days", type=int, default=400)
    sp = ssub.add_parser("themes", help="产业链主题（themes.yaml）与主题 agent 的建议")
    sp.add_argument("--suggest", nargs="*", metavar="THEME_ID", help="运行主题 agent（可指定主题 id，不填表示全部）")
    sp.add_argument("--accept", type=int, metavar="ID", help="采纳一条建议（写入 themes.yaml）")
    sp.add_argument("--dismiss", type=int, metavar="ID", help="忽略一条建议")
    scp.set_defaults(func=cmd_screen)

    fp = sub.add_parser("fund", help="模拟基金：按策略生成盘前计划、盘中提醒、盘后复盘（模拟组合信号，非投资建议；不对接券商、不下单）")
    fsub = fp.add_subparsers(dest="fund_cmd", required=True)
    sp = fsub.add_parser("sync", help="把 funds.yaml 的基金定义同步到数据库（新基金按初始资金建账）")
    sp.add_argument("--dry-run", action="store_true", help="只显示会做的变更")
    sp = fsub.add_parser("refresh", help="晚间：刷新指数、汇率与股票池日线（逐只限速，可中断续跑）、港股每手股数")
    sp.add_argument("--market", choices=["HK", "A"])
    sp.add_argument("--limit", type=int, help="本次最多请求多少只（试跑用）")
    sp.add_argument("--dry-run", action="store_true", help="只估算请求数，不联网")
    for name, hlp in (("plan", "盘前计划：按收盘数据计算当日操作建议（只读库，不联网）"),
                      ("close", "盘后：按当日日线模拟成交、扣费，记净值与回撤熔断")):
        sp = fsub.add_parser(name, help=hlp)
        sp.add_argument("fund", nargs="*", help="基金 id，不填表示全部")
        sp.add_argument("--date", help="日期 YYYY-MM-DD（默认今天，北京时间）")
        sp.add_argument("--push", action="store_true", help="推送")
        sp.add_argument("--dry-run", action="store_true", help="不写库、不推送，只打印")
    sp = fsub.add_parser("alert", help="盘中：只查持仓与当天条件单的快照报价，止损、突破、跳空、证伪条件触发时提醒")
    sp.add_argument("--push", action="store_true", help="推送")
    sp.add_argument("--dry-run", action="store_true", help="不记录已提醒、不推送")
    sp = fsub.add_parser("weekly", help="周报：净值、超额收益、回撤、胜率与盈亏比")
    sp.add_argument("--date", help="日期 YYYY-MM-DD（默认今天）")
    sp.add_argument("--push", action="store_true", help="推送")
    sp.add_argument("--dry-run", action="store_true", help="不推送，只打印")
    fsub.add_parser("status", help="查看基金净值、持仓、待执行建议与最近打分")
    sp = fsub.add_parser("resume", help="恢复因回撤三档熔断而暂停的基金")
    sp.add_argument("fund_id")
    sp.add_argument("--dry-run", action="store_true")
    fp.set_defaults(func=cmd_fund)

    sp = sub.add_parser("web", help="WebUI（只监听 127.0.0.1）：事件与估值修正时间线、观察清单、追踪、模型与日程配置；内置调度器")
    sp.add_argument("--port", type=int, help="端口（默认 config.yaml 的 web.port，8765）")
    sp.add_argument("--no-scheduler", action="store_true", help="不启动内置调度器与后台任务执行器（WebUI 里提交的任务会一直排队）")
    sp.add_argument("--jobs-only", action="store_true", help="只启动后台任务执行器，不启动内置调度器：测试实例用，WebUI 里提交的任务会执行，但不会按日程自动运行")
    sp.add_argument("--open", action="store_true", help="启动后在浏览器打开")
    sp.set_defaults(func=cmd_web)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
