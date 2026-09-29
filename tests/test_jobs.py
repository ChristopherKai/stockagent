"""后台任务与内置调度器：任务登记表的参数校验、执行器的互斥与退出状态、cron 匹配与补跑。"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta

import pytest

from stockagent import jobrun, jobs, scheduler
from stockagent.config import SchedulerConfig
from stockagent.cron import Cron
from stockagent.utils import CST, file_lock, lock_holder


# ---------------------------------------------------------------- cron

@pytest.mark.parametrize("expr,when,hit", [
    ("*/20 9-16 * * 1-5", datetime(2026, 9, 28, 9, 40), True),     # 周一
    ("*/20 9-16 * * 1-5", datetime(2026, 9, 27, 9, 40), False),    # 周日
    ("*/20 9-16 * * 1-5", datetime(2026, 9, 28, 17, 0), False),
    ("30 8 * * 1-5", datetime(2026, 9, 29, 8, 30), True),
    ("0 20 * * 6", datetime(2026, 10, 3, 20, 0), True),            # 周六
    ("0 0 * * 7", datetime(2026, 10, 4, 0, 0), True),              # 7 也是周日
    ("0 0 1 * 1", datetime(2026, 10, 1, 0, 0), True),              # 日与周都限定时按「或」：1 号（周四）
    ("0 0 1 * 1", datetime(2026, 10, 5, 0, 0), True),              # 周一
    ("0 0 1 * 1", datetime(2026, 10, 6, 0, 0), False),
    ("5/15 * * * *", datetime(2026, 10, 6, 3, 35), True),
])
def test_cron_match(expr, when, hit):
    assert Cron(expr).match(when.replace(tzinfo=CST)) is hit


def test_cron_next_and_errors():
    assert Cron("*/20 9-16 * * 1-5").next_after(datetime(2026, 9, 26, 17, 5, tzinfo=CST)) == datetime(2026, 9, 28, 9, 0, tzinfo=CST)
    for bad in ("30 8 * *", "61 * * * *", "* 25 * * *", "*/0 * * * *", "a * * * *", "5-1 * * * *"):
        with pytest.raises(ValueError):
            Cron(bad)
    with pytest.raises(ValueError):
        SchedulerConfig.model_validate({"jobs": [{"name": "a", "task": "run", "cron": "30 8 * *"}]})


# ---------------------------------------------------------------- 任务登记表

def test_build_validates_arguments(app):
    wl, profiles = app.watchlist, app.settings.llm.profiles
    steps, title, cid = jobs.build(wl, profiles, "run", ["acme"], ["--push"])
    assert steps == [["run", "--push", "acme"]] and cid == "acme" and "测试公司" in title
    assert jobs.build(wl, profiles, "target_run", [], [])[0] == [["target", "run"]]
    assert jobs.build(wl, profiles, "llm_test", profile="mock")[0] == [["llm-test", "--profile", "mock"]]
    steps, *_ = jobs.build(wl, profiles, "onboard", ["测试公司"])  # 名称也能识别
    assert steps[0] == ["fetch", "acme"] and steps[-1] == ["target", "add", "acme"] and len(steps) == 6
    for task, companies, flags, profile, msg in [
        ("rm", [], [], None, "不支持的任务"),
        ("run", ["nope"], [], None, "找不到"),
        ("run", [], ["--rm-rf"], None, "不支持的开关"),
        ("target_add", [], [], None, "需要指定公司"),
        ("llm_test", ["acme"], [], "mock", "不接受公司"),
        ("llm_test", [], [], "missing", "没有名为"),
    ]:
        with pytest.raises(jobs.JobError, match=msg):
            jobs.build(wl, profiles, task, companies, flags, profile)


class FakePopen:
    launched: list = []

    def __init__(self, cmd, **kw):
        self.cmd, self.kw, self.pid, self.code = cmd, kw, 424242, None
        FakePopen.launched.append(self)

    def poll(self):
        return self.code


class Holder:
    def __init__(self, app, project):
        self.app, self.root, self.config_path = app, project, project / "config.yaml"


def test_runner_launch_exclusivity_and_env(app, project, monkeypatch):
    FakePopen.launched = []
    monkeypatch.setattr(jobs.subprocess, "Popen", FakePopen)
    (project / ".env").write_text("NEW_API_KEY=abc123\n", encoding="utf-8")
    runner = jobs.JobRunner(Holder(app, project))
    a = jobs.submit(app, "run", flags=["--push"])
    b = jobs.submit(app, "alert")               # 与 run 同属 pipeline 组：等 run 结束
    c = jobs.submit(app, "dossier", ["acme"])
    runner.tick()
    started = [p.cmd[3] for p in FakePopen.launched]
    assert started == [str(a), str(c)]
    p = FakePopen.launched[0]
    assert p.cmd[:3] == [sys.executable, "-m", "stockagent.jobrun"] and p.kw["cwd"] == project
    assert p.kw["env"]["NEW_API_KEY"] == "abc123" and p.kw["env"]["STOCK_JOB_SOURCE"] == "web" and p.kw["start_new_session"]
    assert app.db.job(b)["status"] == "queued" and app.db.job(a)["status"] == "running"
    # 包装进程没写结果就退出：执行器按退出码记录，然后启动排队的同组任务
    FakePopen.launched[0].code = 1
    runner.tick()
    assert app.db.job(a)["status"] == "failed" and app.db.job(b)["status"] == "running"
    # 命令行启动的深度报告占着锁：排队的深度报告不启动
    FakePopen.launched[1].code = 0
    d = jobs.submit(app, "dossier", ["acme"])
    with app.lock("dossier") as ok:
        assert ok
        runner.tick()
        assert app.db.job(d)["status"] == "queued"
    assert runner.cancel(d) and app.db.job(d)["status"] == "cancelled"


def test_runner_recovers_stale_running_jobs(app, project):
    jid = jobs.submit(app, "fetch")
    app.db.claim_job(jid, 999999, "x.log")
    jobs.JobRunner(Holder(app, project)).recover()
    assert app.db.job(jid)["status"] == "interrupted"


def test_jobrun_records_exit_status(app, project, monkeypatch, capsys):
    jid = jobs.submit(app, "llm_test", profile="mock")
    assert jobrun.main([str(jid), "-c", str(project / "config.yaml")]) == 0
    out = capsys.readouterr().out
    assert app.db.job(jid)["status"] == "done" and "[mock] mock" in out and f"【任务 #{jid}】完成" in out
    jid = app.db.add_job("run", [["target", "rebase", "acme"]], "重建基线", "web", company_id="acme")
    assert jobrun.main([str(jid), "-c", str(project / "config.yaml")]) == 0  # 没有深度报告：打印错误但命令本身正常结束
    jid = app.db.add_job("run", [["dossier", "nope"]], "错误的公司", "web")
    assert jobrun.main([str(jid), "-c", str(project / "config.yaml")]) == 1
    assert app.db.job(jid)["status"] == "failed" and app.db.job(jid)["exit_code"] == 1


def test_file_lock_is_exclusive_across_processes(tmp_path):
    path = tmp_path / "locks" / "x.lock"
    code = "import sys, time; from pathlib import Path; from stockagent.utils import file_lock\n" \
           "with file_lock(Path(sys.argv[1])) as ok:\n    print(ok, flush=True); time.sleep(3)"
    p = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "True"
    with file_lock(path) as ok:
        assert ok is False
    assert lock_holder(path) == p.pid
    p.wait()
    with file_lock(path) as ok:
        assert ok is True
    assert lock_holder(path) is None


# ---------------------------------------------------------------- 调度

def _settings_with_jobs(app, jobs_cfg, catchup=120):
    app.settings.scheduler = SchedulerConfig.model_validate({"enabled": True, "catchup_minutes": catchup, "jobs": jobs_cfg})


def test_scheduler_fires_once_and_catches_up(app):
    _settings_with_jobs(app, [{"name": "早盘", "task": "run", "args": ["--push"], "cron": "30 8 * * 1-5"},
                              {"name": "盘中", "task": "alert", "cron": "*/20 9-16 * * 1-5"}])
    t = datetime(2026, 9, 29, 8, 0, tzinfo=CST)  # 周二
    assert scheduler.tick(app, t) == []  # 首次见到：从当前分钟开始计
    msgs = scheduler.tick(app, t.replace(minute=30))
    assert len(msgs) == 1 and "早盘" in msgs[0]
    assert app.db.jobs()[0]["argv"] == [["run", "--push"]] and app.db.jobs()[0]["source"] == "scheduler"
    assert scheduler.tick(app, t.replace(minute=30)) == []  # 同一分钟不重复
    # 电脑睡眠 08:31–10:05：09:00、09:20…10:00 错过的盘中提醒只补跑一次；早盘任务还在排队，盘中提醒同属一组，跳过
    msgs = scheduler.tick(app, datetime(2026, 9, 29, 10, 5, tzinfo=CST))
    assert len(msgs) == 1 and "跳过" in msgs[0]
    app.db.update_job(app.db.jobs()[0]["id"], status="done")
    assert scheduler.tick(app, datetime(2026, 9, 29, 10, 6, tzinfo=CST)) == []  # 已在上一分钟记为触发过
    msgs = scheduler.tick(app, datetime(2026, 9, 29, 10, 20, tzinfo=CST))
    assert len(msgs) == 1 and "盘中" in msgs[0] and "补跑" not in msgs[0]


def test_scheduler_catchup_window(app):
    _settings_with_jobs(app, [{"name": "早盘", "task": "run", "cron": "30 8 * * 1-5"}], catchup=60)
    t = datetime(2026, 9, 29, 8, 0, tzinfo=CST)
    scheduler.tick(app, t)
    msgs = scheduler.tick(app, datetime(2026, 9, 29, 9, 10, tzinfo=CST))  # 错过 40 分钟：补跑
    assert len(msgs) == 1 and "补跑 09-29 08:30" in msgs[0]
    app.db.update_job(app.db.jobs()[0]["id"], status="done")
    assert scheduler.tick(app, datetime(2026, 9, 30, 10, 0, tzinfo=CST)) == []  # 错过 90 分钟：超出窗口，不补跑
    runs = scheduler.next_runs(app, datetime(2026, 9, 30, 10, 0, tzinfo=CST))
    assert runs[0]["next"] == datetime(2026, 10, 1, 8, 30, tzinfo=CST)


def test_scheduler_rejects_bad_job_config(app):
    _settings_with_jobs(app, [{"name": "坏", "task": "target_add", "cron": "* * * * *"}])  # 开始追踪必须指定公司
    t = datetime(2026, 9, 29, 8, 0, tzinfo=CST)
    scheduler.tick(app, t)
    msgs = scheduler.tick(app, t + timedelta(minutes=1))
    assert msgs and "配置有误" in msgs[0] and app.db.jobs() == []
