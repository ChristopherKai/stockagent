"""命令行入口：stock <命令>。"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from . import app as appmod
from . import brief, docs, dossier, evaluate, monitor, notify, profile, tracker, triage
from .app import SOURCE_LABEL, App
from .config import ConfigError
from .llm import LLMError
from .utils import fmt_local, iso, lock_holder, today_local, utcnow
from .valuation import TARGET_LABEL, describe, fmt

LLM_TASKS = ("triage", "stock_brief", "market_brief", "profile", "research", "dossier", "monitor", "tracker")


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
            msgs += docs.fetch_reports(app.db, app.session, app.data_dir, c)
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
    print("6/6 ", end="")
    _brief(app, companies, use_llm, args.push, args.open, tracked=res.notable)
    print(f"完成，用时 {time.monotonic() - t0:.0f} 秒")


def cmd_alert(args) -> None:
    app = _load(args)
    with app.lock("pipeline") as ok:
        if not ok:
            print(PIPELINE_BUSY)
            return
        companies = app.watchlist.select(args.company)
        _print(appmod.fetch(app, companies, market=False))
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
        for t in app.db.triggers(c.id):
            print(f"  [{brief.STATUS_LABEL[t['status']]}] {t['description']}　→ {dossier.ACTION_LABEL[t['action']]}"
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
    print(describe(s["live_model"], app.settings.tracker.target))
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


def cmd_web(args) -> None:
    from .web.server import serve  # noqa: PLC0415 - 只有 stock web 需要 FastAPI

    try:
        serve(args.config, port=args.port, background=not args.no_scheduler, open_browser=args.open)
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
            _print(docs.fetch_reports(app.db, app.session, app.data_dir, c) or [f"{c.name}：定期报告已是最新"])
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
    chans = notify.channels(app.settings)
    print("推送渠道：" + ("、".join(chans) if chans else "未配置"))


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

    sp = with_company(sub.add_parser("requests", help="查看或关闭「需要你补充的数据」请求"))
    sp.add_argument("--done", type=int, metavar="编号", help="标记已补充")
    sp.add_argument("--dismiss", type=int, metavar="编号", help="忽略该请求")
    sp.set_defaults(func=cmd_requests)

    dp = sub.add_parser("docs", help="文档：fetch 自动下载定期报告 / add 手动添加 / list 查看")
    dsub = dp.add_subparsers(dest="docs_cmd", required=True)
    with_company(dsub.add_parser("fetch", help="下载最新年报和中期报告"))
    sp = dsub.add_parser("add", help="手动添加文档（PDF/DOCX/TXT/MD/HTML）")
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

    sp = sub.add_parser("llm-test", help="测试 LLM 配置（地址、模型、JSON 输出）")
    sp.add_argument("--profile")
    sp.set_defaults(func=cmd_llm_test)

    sub.add_parser("status", help="查看数据与配置状态").set_defaults(func=cmd_status)

    sp = sub.add_parser("web", help="WebUI（只监听 127.0.0.1）：事件与估值修正时间线、观察清单、追踪、模型与日程配置；内置调度器")
    sp.add_argument("--port", type=int, help="端口（默认 config.yaml 的 web.port，8765）")
    sp.add_argument("--no-scheduler", action="store_true", help="不启动内置调度器与后台任务执行器")
    sp.add_argument("--open", action="store_true", help="启动后在浏览器打开")
    sp.set_defaults(func=cmd_web)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
