"""后台任务：WebUI 与内置调度器只能执行登记表中的命令。

- 命令与参数固定（argv 列表，不经过 shell）；公司参数必须是观察清单里的 id，开关必须在登记表里。
- 录入消息、补充数据等自由文本输入不走子进程，在 web 进程内直接调用函数完成（文本放进 argv 可能被当成选项）。
- 任务在子进程中运行（python -m stockagent.jobrun <编号>），输出写入 data/jobs/<编号>.log；
  包装进程自己把退出状态写回数据库，所以 web 重启也不会丢失任务结果。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from .utils import iso, lock_holder, log, utcnow


@dataclass(frozen=True)
class Task:
    key: str
    title: str
    argv: tuple[str, ...]              # 子命令，如 ("run",)、("docs", "fetch")、("target", "run")
    flags: tuple[str, ...] = ()        # 允许的开关
    companies: str = "any"             # none 不接受；any 可选（不填表示全部）；many 至少一家
    group: str | None = None           # 互斥组：同组同时只运行一个（pipeline 与 run/alert/target run 的进程锁对应）
    profile: bool = False              # 接受 --profile NAME（llm-test）
    steps: tuple[tuple[str, ...], ...] = ()  # 组合任务：依次执行的子命令（每步都带上公司参数）
    category: str = "常用"              # WebUI 分组
    desc: str = ""                     # WebUI 上的一句话说明
    listing: bool = False              # 接受 市场 + 代码（新增公司；在观察清单页提交，不出现在任务页和日程里）


TASKS: dict[str, Task] = {t.key: t for t in [
    Task("run", "日常流程", ("run",), ("--push", "--no-llm"), "any", "pipeline",
         desc="每天定时运行的就是它：抓取 → 分级 → 证伪检查 → 标的追踪（修正估值）→ 简报。平时只需要这一个"),
    Task("target_run", "标的追踪：按新事件修正估值", ("target", "run"), ("--no-fetch", "--push"), "any", "pipeline",
         desc="只处理追踪中的公司：抓取、分级、证伪检查后修正估值。标的页面的「立即检查」就是它"),
    Task("dossier", "深度报告", ("dossier",), ("--reuse-notes", "--refill"), "any", "dossier",
         desc="4 个研究 agent 并行研究后撰写报告，每家数分钟；不选公司表示全部；同一时间只运行一个"),
    Task("alert", "盘中提醒", ("alert",), ("--no-push",), "any", "pipeline",
         desc="盘中抓取公告和新闻并分级，推送重要事件与触发的证伪条件（不修正估值）"),
    Task("fetch", "抓取行情、公告、新闻", ("fetch",), ("--no-prices", "--no-filings", "--no-news"), "any", category="资料与分级",
         desc="日常流程的第一步；单独运行时不调用模型"),
    Task("triage", "事件分级", ("triage",), (), "any", category="资料与分级", desc="给还没分级的新闻和公告打分（重要性、方向）"),
    Task("docs_fetch", "下载最新年报 / 中期报告", ("docs", "fetch"), (), "any", category="资料与分级"),
    Task("profile", "生成公司画像", ("profile",), ("--force",), "any", category="资料与分级", desc="读年报生成公司画像（研究背景）"),
    Task("brief", "生成简报", ("brief",), ("--push", "--no-llm"), "any", category="资料与分级", desc="只生成简报，不抓取"),
    Task("target_add", "开始追踪（建立估值基线）", ("target", "add"), ("--dossier",), "many", category="追踪管理",
         desc="用深度报告的估值矩阵建立基线（代码计算，不调用模型）；勾选「缺报告时先生成」会先生成深度报告"),
    Task("target_rebase", "用最新深度报告重建估值基线", ("target", "rebase"), (), "many", category="追踪管理"),
    Task("onboard", "新公司初始化", (), (), "many", "dossier", category="追踪管理",
         steps=(("fetch",), ("docs", "fetch"), ("triage",), ("profile",), ("dossier",), ("target", "add")),
         desc="依次运行：抓取 → 下载年报 → 分级 → 画像 → 深度报告 → 开始追踪"),
    Task("add_company", "新增公司并初始化", ("add",), (), "none", "dossier", category="追踪管理", listing=True,
         desc="按市场与代码查公司资料写入观察清单，再依次抓取 → 下载年报 → 分级 → 画像 → 深度报告 → 开始追踪"),
    Task("screen_run", "选股器", ("screen", "run"), ("--long-only", "--event-only", "--no-llm", "--push"), "none", "screen",
         category="选股", desc="港股通 + A 股全市场初筛：刷新数据 → 代码打分 → 模型快筛；长线池每周、事件池每个交易日"),
    Task("screen_refresh", "选股器：刷新数据", ("screen", "refresh"), (), "none", "screen", category="选股",
         desc="只刷新股票池、报表与全市场公告事件，不打分"),
    Task("screen_themes", "选股器：主题 agent", ("screen", "themes", "--suggest"), (), "none", "screen", category="选股",
         desc="按 themes.yaml 的产业链主题找瓶颈环节的公司，结果是建议，在选股页采纳后才写入主题文件"),
    Task("llm_test", "测试模型连接", ("llm-test",), (), "none", profile=True, category="维护", desc="逐个测试文本与 JSON 输出"),
    Task("eval", "事后检验", ("eval",), (), "any", category="维护", desc="统计已分级事件发布后的超额收益，检验分级有没有预测力"),
]}
CATEGORIES = ["常用", "资料与分级", "追踪管理", "选股", "维护"]
FLAG_LABEL = {"--push": "推送", "--no-llm": "不调用模型", "--no-fetch": "不抓取（只处理已分级的依据）", "--force": "强制重做",
              "--reuse-notes": "复用研究笔记", "--refill": "重新补缺", "--dossier": "缺报告时先生成", "--no-prices": "不抓行情",
              "--no-filings": "不抓公告", "--no-news": "不抓新闻", "--no-push": "不推送",
              "--long-only": "只跑长线池", "--event-only": "只跑事件池"}


class JobError(ValueError):
    pass


def build(watchlist, profiles, task: str, companies: list[str] | None = None, flags: list[str] | None = None,
          profile: str | None = None, verbose: bool = False, listing: tuple[str, str] | None = None,
          ) -> tuple[list[list[str]], str, str | None]:
    """校验并生成任务的步骤 argv（不含 -c 配置路径）；返回 (步骤, 标题, 公司 id)。"""
    t = TASKS.get(task)
    if t is None:
        raise JobError(f"不支持的任务：{task}")
    if t.listing:
        from .newco import normalize

        if listing is None:
            raise JobError(f"{t.title} 需要市场与代码")
        try:
            market, code = normalize(*listing)
        except ValueError as e:
            raise JobError(str(e)) from e
        return [[*(["-v"] if verbose else []), *t.argv, market, code, "--onboard"]], f"{t.title}：{market} {code}", None
    if listing is not None:
        raise JobError(f"{t.title} 不接受市场与代码")
    ids: list[str] = []
    for key in companies or []:
        c = watchlist.get(key)
        if c is None:
            raise JobError(f"观察清单中找不到：{key}")
        if c.id.startswith("-"):
            raise JobError(f"公司 id 不能以 - 开头：{c.id}")
        if c.id not in ids:
            ids.append(c.id)
    if t.companies == "none" and ids:
        raise JobError(f"{t.title} 不接受公司参数")
    if t.companies == "many" and not ids:
        raise JobError(f"{t.title} 需要指定公司")
    bad = [f for f in flags or [] if f not in t.flags]
    if bad:
        raise JobError(f"{t.title} 不支持的开关：{' '.join(bad)}")
    opts = list(dict.fromkeys(flags or []))
    if t.profile:
        if profile is None or profile not in profiles:
            raise JobError(f"没有名为 {profile} 的模型配置")
        opts += ["--profile", profile]
    head = ["-v"] if verbose else []
    steps = [[*head, *s, *ids] for s in t.steps] if t.steps else [[*head, *t.argv, *opts, *ids]]
    names = "、".join(watchlist.get(i).name for i in ids)
    title = t.title + (f"：{names}" if names else "") + (f"（{profile}）" if t.profile else "")
    return steps, title, ids[0] if len(ids) == 1 else None


def submit(app, task: str, companies: list[str] | None = None, flags: list[str] | None = None,
           profile: str | None = None, source: str = "web", note: str | None = None, verbose: bool = False,
           listing: tuple[str, str] | None = None) -> int:
    steps, title, cid = build(app.watchlist, app.settings.llm.profiles, task, companies, flags, profile, verbose, listing)
    return app.db.add_job(task, steps, title, source, company_id=cid, note=note)


def busy(app, task: str) -> dict | None:
    """同一互斥组（或同一任务）已在排队或运行的任务。"""
    t = TASKS.get(task)
    for j in app.db.jobs(limit=200, statuses=("queued", "running")):
        jt = TASKS.get(j["task"])
        if j["task"] == task or (t and t.group and jt and jt.group == t.group):
            return j
    return None


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class JobRunner:
    """web 进程里的任务执行线程：按互斥规则启动排队的任务，回收结束的子进程，支持取消。"""

    def __init__(self, holder, poll: float = 2.0):
        self.holder = holder          # AppHolder：.app（当前 App）、.config_path、.root
        self.poll = poll
        self.procs: dict[int, subprocess.Popen] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def app(self):
        return self.holder.app

    def start(self) -> None:
        self.recover()
        self._thread = threading.Thread(target=self._loop, name="job-runner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self.poll):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 执行线程不能因为一次异常退出
                log.exception("任务执行器出错")

    def recover(self) -> None:
        """上次 web 进程留下的「运行中」任务：进程还在的继续由包装进程记录结果；进程已不在的标为中断。"""
        for j in self.app.db.jobs(limit=500, statuses=("running",)):
            if not pid_alive(j["pid"]):
                self.app.db.update_job(j["id"], status="interrupted", finished_at=iso(utcnow()))

    def tick(self) -> None:
        with self._lock:
            self._reap()
            self._launch_queued()

    def _reap(self) -> None:
        for jid, p in list(self.procs.items()):
            code = p.poll()
            if code is None:
                continue
            del self.procs[jid]
            j = self.app.db.job(jid)
            if j and j["status"] == "running":  # 包装进程没来得及写结果（例如被强制结束）
                self.app.db.update_job(jid, status="failed" if code else "done", exit_code=code, finished_at=iso(utcnow()))

    def _group_busy(self, group: str | None, running: list[dict]) -> bool:
        if group is None:
            return False
        if any(TASKS.get(j["task"]) and TASKS[j["task"]].group == group for j in running):
            return True
        # 命令行（cron、手动）启动的同组任务：看进程锁
        return lock_holder(self.app.lock_path(group)) is not None

    def _launch_queued(self) -> None:
        running = self.app.db.jobs(limit=200, statuses=("running",))
        queued = list(reversed(self.app.db.jobs(limit=200, statuses=("queued",))))  # 先进先出
        for j in queued:
            if len(running) >= self.app.settings.web.max_jobs:
                return
            t = TASKS.get(j["task"])
            if self._group_busy(t.group if t else None, running):
                continue
            if self._launch(j):
                running.append(self.app.db.job(j["id"]))

    def _launch(self, job: dict) -> bool:
        app = self.app
        jid = job["id"]
        log_dir = app.data_dir / "jobs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{jid}.log"
        if not app.db.claim_job(jid, 0, str(log_path)):
            return False  # 已被取消
        env = dict(os.environ)
        env.update({k: v for k, v in dotenv_values(Path(self.holder.root) / ".env").items() if v is not None})
        env.update(PYTHONUNBUFFERED="1", STOCK_JOB_SOURCE=job["source"])
        cmd = [sys.executable, "-m", "stockagent.jobrun", str(jid), "-c", str(self.holder.config_path)]
        try:
            with open(log_path, "ab") as out:
                p = subprocess.Popen(cmd, cwd=self.holder.root, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     env=env, start_new_session=True)
        except OSError as e:
            app.db.update_job(jid, status="failed", exit_code=-1, finished_at=iso(utcnow()), note=f"无法启动：{e}")
            return False
        self.procs[jid] = p
        app.db.update_job(jid, pid=p.pid)
        return True

    def cancel(self, job_id: int) -> bool:
        j = self.app.db.job(job_id)
        if j is None:
            return False
        if j["status"] == "queued":
            self.app.db.update_job(job_id, status="cancelled", finished_at=iso(utcnow()))
            return True
        if j["status"] == "running" and j["pid"]:
            try:
                os.killpg(j["pid"], signal.SIGTERM)  # 包装进程收到后记为已取消
            except ProcessLookupError:
                self.app.db.update_job(job_id, status="interrupted", finished_at=iso(utcnow()))
            return True
        return False


def read_log(path: str | None, offset: int = 0, limit: int = 200_000) -> tuple[str, int]:
    """从 offset 开始读取任务日志，返回 (新内容, 新的 offset)。"""
    if not path or not Path(path).exists():
        return "", offset
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(limit)
    return data.decode("utf-8", errors="replace"), offset + len(data)
