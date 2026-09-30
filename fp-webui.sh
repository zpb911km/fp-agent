#!/usr/bin/env bash
# 本仓库（feat/async-proactive-agent 分支）的 fp-webui 启动器 — 同 fp.sh 原理
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for d in fp-core fp fp-webui fp-acp fp-terminal; do
  src="$ROOT/packages/$d/src"
  [ -d "$src" ] && PYTHONPATH="$src${PYTHONPATH:+:$PYTHONPATH}"
done
export PYTHONPATH
exec fp-webui "$@"
