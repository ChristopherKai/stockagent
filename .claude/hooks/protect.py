"""PreToolUse(Edit|Write)：拦截对 .env / uv.lock / data/ 的直接修改。

.env 存放 API key 与推送 webhook；data/ 是生产库与生成产物（stock web 的调度器在写）；
uv.lock 只应由 uv add / uv sync 更新。exit 2 = 阻止该次工具调用，stderr 回给 Claude。
"""
import json
import os
import re
import sys

path = json.load(sys.stdin).get("tool_input", {}).get("file_path", "")
if not path:
    sys.exit(0)
path = os.path.realpath(path)
root = os.path.realpath(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())

if re.search(r"(^|/)(\.env|uv\.lock)$", path) or path.startswith(root + "/data/"):
    print(f"受保护文件，未修改：{path}\n.env / uv.lock / data/ 请手动处理（uv.lock 用 uv add / uv sync 更新）。", file=sys.stderr)
    sys.exit(2)
