"""Stop hook：src/ 里有 .py 比正在运行的 `stock web` 进程更新时，提醒用户重启 web（只提示，不拦截、不自动重启）。

`stock web`（调度器 + 任务执行器）跑在用户自己的终端里，启动后不会热加载代码；
改完 src/ 却不重启，调度和 WebUI 仍是旧代码。输出 systemMessage 给用户看，exit 0。
"""
import json
import os
import subprocess
import sys
import time

root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def web_pids() -> list[int]:
    """主仓库 .venv 里的 `stock web` 进程（不含 jobrun、uv 包装进程、其他工作树的测试实例）。"""
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    pids = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if cmd.startswith(f"{root}/.venv/bin/python") and cmd.endswith("/bin/stock web"):
            pids.append(int(pid))
    return pids


def started_at(pid: int) -> float | None:
    """macOS 的 ps 没有 etimes，解析 etime（[[dd-]hh:]mm:ss）。"""
    out = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    if not out:
        return None
    days, _, rest = out.rpartition("-")
    secs = 0
    for part in rest.split(":"):
        secs = secs * 60 + int(part)
    return time.time() - secs - int(days or 0) * 86400


def newest_src() -> tuple[float, str]:
    best = (0.0, "")
    for base, _, files in os.walk(os.path.join(root, "src")):
        for f in files:
            if f.endswith((".py", ".html", ".js", ".css")):
                p = os.path.join(base, f)
                m = os.path.getmtime(p)
                if m > best[0]:
                    best = (m, os.path.relpath(p, root))
    return best


try:
    json.load(sys.stdin)
except Exception:
    pass

pids = web_pids()
if not pids:
    sys.exit(0)
start = min(filter(None, (started_at(p) for p in pids)), default=None)
mtime, path = newest_src()
if start is not None and mtime > start + 2:
    print(json.dumps({"systemMessage": f"⚠ src/ 有改动（最新：{path}）晚于 stock web 的启动时间，需要重启 web 才生效（pid {pids[0]}，在你的终端里 Ctrl-C 后重新 uv run stock web）。"}, ensure_ascii=False))
