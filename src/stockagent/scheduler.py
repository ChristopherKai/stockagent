"""内置调度器（随 stock web 运行）：按 config.yaml 的 scheduler.jobs（5 段 cron，北京时间）把任务放进队列。

- 每分钟检查一次；kv 键 sched_last:<名称> 记录已触发到的分钟，重启后不会重复触发。
- 电脑睡眠等原因错过的时点：醒来后在 catchup_minutes 内的补跑一次，更早的跳过。
- 同一任务或同一互斥组已在排队或运行时，这次跳过（记日志）。
- 与系统 cron 二选一，不要同时启用。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

from . import jobs
from .cron import Cron
from .utils import CST, log


def _minute(dt: datetime) -> datetime:
    return dt.astimezone(CST).replace(second=0, microsecond=0)


def _key(name: str) -> str:
    return f"sched_last:{name}"


def split_args(args: list[str]) -> tuple[list[str], list[str]]:
    """日程参数里 -- 开头的是开关，其余是公司 id。"""
    return [a for a in args if a.startswith("--")], [a for a in args if not a.startswith("--")]


def due(app, now: datetime | None = None) -> list[tuple[object, datetime]]:
    """到期的日程：[(日程, 触发的时点)]。首次见到的日程只从当前分钟开始计，不补跑以前的。"""
    cfg = app.settings.scheduler
    now = _minute(now or datetime.now(CST))
    out = []
    for job in cfg.jobs:
        if not job.enabled:
            continue
        last_s = app.db.kv_get(_key(job.name))
        start = now - timedelta(minutes=cfg.catchup_minutes)
        if last_s:
            start = max(start, datetime.fromisoformat(last_s) + timedelta(minutes=1))
        else:
            start = now
        cron = Cron(job.cron)
        t, hit = now, None
        while t >= start:  # 从最近往前找，只补跑一次
            if cron.match(t):
                hit = t
                break
            t -= timedelta(minutes=1)
        if hit:
            out.append((job, hit))
    return out


def tick(app, now: datetime | None = None) -> list[str]:
    """检查一次：到期的日程放进任务队列，返回日志。"""
    if not app.settings.scheduler.enabled:
        return []
    now = _minute(now or datetime.now(CST))
    for job in app.settings.scheduler.jobs:  # 第一次见到的日程从当前分钟开始计，不补跑以前的
        if job.enabled and not app.db.kv_get(_key(job.name)):
            app.db.kv_set(_key(job.name), (now - timedelta(minutes=1)).isoformat())
    msgs = []
    for job, hit in due(app, now):
        app.db.kv_set(_key(job.name), now.isoformat())
        late = f"（补跑 {hit:%m-%d %H:%M} 的日程）" if hit < now else ""
        if running := jobs.busy(app, job.task):
            msgs.append(f"日程「{job.name}」跳过：任务 #{running['id']}（{running['title']}）尚未结束")
            continue
        flags, companies = split_args(job.args)
        try:
            jid = jobs.submit(app, job.task, companies, flags, source="scheduler", note=f"日程「{job.name}」{late}")
        except jobs.JobError as e:
            msgs.append(f"日程「{job.name}」配置有误：{e}")
            continue
        msgs.append(f"日程「{job.name}」→ 任务 #{jid}{late}")
    return msgs


def next_runs(app, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(CST)
    out = []
    for job in app.settings.scheduler.jobs:
        try:
            nxt = Cron(job.cron).next_after(now) if job.enabled else None
        except ValueError:
            nxt = None
        out.append({"job": job, "next": nxt, "last": app.db.kv_get(_key(job.name))})
    return out


class Scheduler:
    def __init__(self, holder):
        self.holder = holder
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for m in tick(self.holder.app):
                    log.warning("调度：%s", m)
            except Exception:  # noqa: BLE001 - 调度线程不能因为一次异常退出
                log.exception("调度器出错")
            now = datetime.now(CST)
            self._stop.wait(60 - now.second + 1)  # 睡到下一分钟的第 1 秒
