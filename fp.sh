#!/usr/bin/env bash
# 本仓库（feat/async-proactive-agent 分支）的 fp 启动器
#
# 默认 `fp` 命令经 editable 指向旧仓库 agent/（dev），本脚本把本仓库
# 各包的 src 前置到 PYTHONPATH（优先级高于 site-packages 的 editable），
# 因此 exec 的 fp/fp-webui/fp-acp 跑的都是**本仓库的新分支代码**。
# 用法：./fp.sh [fp 参数...]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for d in fp-core fp fp-webui fp-acp fp-terminal; do
  src="$ROOT/packages/$d/src"
  [ -d "$src" ] && PYTHONPATH="$src${PYTHONPATH:+:$PYTHONPATH}"
done
export PYTHONPATH
exec fp "$@"
