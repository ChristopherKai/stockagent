#!/bin/bash
# Stop hook：src/ 或 tests/ 有未提交改动时跑一遍测试，用死代理证明测试离线（CLAUDE.md「Tests stay offline」）。
# 失败 → exit 2，最后 30 行输出回给 Claude 继续修；只强制重试一次（stop_hook_active），避免死循环。
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

grep -q '"stop_hook_active": *true' && exit 0
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
git status --porcelain -- src tests 2>/dev/null | grep -q . || exit 0
command -v uv >/dev/null || exit 0

out=$(HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 NO_PROXY=127.0.0.1,localhost \
      uv run pytest -q -x 2>&1) && exit 0
echo "离线测试未通过（uv run pytest -q -x，死代理 127.0.0.1:9）：" >&2
echo "$out" | tail -30 >&2
exit 2
