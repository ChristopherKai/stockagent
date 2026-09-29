"""stock web 的任务包装进程：python -m stockagent.jobrun <任务编号> -c <配置文件>。

按任务记录的步骤依次在本进程内调用 CLI，把退出状态写回数据库（web 进程重启也不影响任务结果）。
收到 SIGTERM（在 WebUI 里取消）时记为已取消。
"""

from __future__ import annotations

import argparse
import signal
import sys
import traceback

from . import cli
from .app import App
from .utils import iso, utcnow

CANCELLED = 143


def _cancel(signum, frame):  # noqa: ARG001
    raise SystemExit(CANCELLED)


def run_step(argv: list[str]) -> int:
    try:
        cli.main(argv)
        return 0
    except SystemExit as e:
        if isinstance(e.code, str):  # sys.exit("说明")：说明写进日志，按失败处理
            print(e.code, flush=True)
            return 1
        return int(e.code or 0)
    except KeyboardInterrupt:
        return CANCELLED
    except Exception:  # noqa: BLE001 - 任何异常都要落到任务状态里
        traceback.print_exc()
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stockagent.jobrun")
    ap.add_argument("job_id", type=int)
    ap.add_argument("-c", "--config", default="config.yaml")
    args = ap.parse_args(argv)
    app = App.load(args.config)
    job = app.db.job(args.job_id)
    if job is None:
        print(f"没有编号为 {args.job_id} 的任务", flush=True)
        return 1
    signal.signal(signal.SIGTERM, _cancel)
    steps = job["argv"]
    code = 0
    print(f"【任务 #{job['id']}】{job['title']}（{iso(utcnow())} 开始）", flush=True)
    try:
        for i, step in enumerate(steps, 1):
            if len(steps) > 1:
                print(f"\n== 第 {i}/{len(steps)} 步：stock {' '.join(step)}", flush=True)
            code = run_step(["-c", args.config, *step])
            if code:
                break
    except SystemExit as e:  # 步骤之间收到取消
        code = int(e.code or 0)
    status = "done" if code == 0 else ("cancelled" if code == CANCELLED else "failed")
    print(f"\n【任务 #{job['id']}】{ {'done': '完成', 'failed': '失败', 'cancelled': '已取消'}[status] }（退出码 {code}）", flush=True)
    app.db.update_job(job["id"], status=status, exit_code=code, finished_at=iso(utcnow()))
    return code


if __name__ == "__main__":
    sys.exit(main())
