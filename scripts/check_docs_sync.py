#!/usr/bin/env python3
"""check_docs_sync.py — 代码变更 → 文档跟进提醒（pre-commit 钩子入口）

机制（以「变更流」为核心，不做文本比对）：
    文档滞后 = 代码发生了会影响文档的变更，而关联文档没有同步变更。

    判定不依赖任何「当前文档对不对」的文本匹配，而是跟着 git diff 走：
      1. 取变更文件清单（提交时=暂存区；主动触发=--since REF）
      2. 代码文件变更 → 通过内置路径规则反查「受影响文档」
      3. 受影响文档在本次变更中没变 → 输出提醒；提交场景默认阻断（exit 1）

    这样新增/改名/重组都不怕：不需要预知任何名称，跟着 diff 走即可。

用法:
    python scripts/check_docs_sync.py                  # pre-commit：检查暂存区
    python scripts/check_docs_sync.py --since HEAD~3   # 主动触发：最近 3 次提交
    python scripts/check_docs_sync.py --repo PATH      # 指定仓库根（测试用）

环境变量:
    FP_DOCS_SYNC_ALLOW=1  显式放行：跳过阻断（exit 0），仅输出提醒

退出码:
    0  无代码变更 / 关联文档已同步 / 显式放行
    1  存在「代码变了但关联文档没变」的提醒（提交场景将阻断）
"""

import argparse
import fnmatch
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── 路径规则：代码 glob → 受影响文档（相对仓库根） ──────────────────────
# 这是「代码文件 → 该去哪些文档确认」的启发式映射。
# 按语义对应关系维护；误报时用 FP_DOCS_SYNC_ALLOW=1 显式放行。
DOC_RULES: list[tuple[str, list[str]]] = [
    # 命令
    (
        "packages/fp-core/src/fp_core/commands/*.py",
        ["docs/guide/命令参考.md", "docs/dev/命令系统.md", "docs/self/扩展命令.md", "README.md"],
    ),
    # 工具
    (
        "packages/fp-core/src/fp_core/tools/*.py",
        ["docs/guide/工具概览.md", "docs/dev/工具系统.md", "docs/self/扩展工具.md", "README.md"],
    ),
    # 生命周期钩子
    (
        "packages/fp-core/src/fp_core/core/lifecycle.py",
        ["docs/guide/插件系统.md", "docs/dev/插件系统.md", "docs/self/扩展插件.md", "README.md"],
    ),
    # 核心引擎其它
    (
        "packages/fp-core/src/fp_core/core/*.py",
        ["docs/dev/架构设计.md", "docs/dev/引擎.md", "docs/dev/项目概览.md"],
    ),
    # 其余 fp_core（会话/配置/记忆/上下文节省…）
    (
        "packages/fp-core/src/fp_core/**/*.py",
        ["docs/dev/*.md"],
    ),
    # FP 入口（CLI 分发）
    (
        "packages/fp/src/fp/**/*.py",
        ["docs/guide/CLI入门.md", "docs/guide/快速开始.md", "docs/dev/项目概览.md"],
    ),
    # 终端界面
    (
        "packages/fp-terminal/**/*.py",
        ["docs/guide/CLI入门.md", "docs/dev/显示层.md"],
    ),
    # WebUI
    (
        "packages/fp-webui/**/*.py",
        ["docs/guide/WebUI手册.md"],
    ),
    # ACP 协议
    (
        "packages/fp-acp/**/*.py",
        ["docs/acp/README.md"],
    ),
    # 开发/脚本工具
    (
        "scripts/*.py",
        ["docs/CONTRIBUTING.md"],
    ),
    # 工程配置（pre-commit / pyproject）
    (
        ".pre-commit-config.yaml",
        ["docs/CONTRIBUTING.md"],
    ),
]

# 文档文件本身（变更它们不算「代码变更」）
_DOC_PATTERNS = ("README.md", "docs/*", "docs/**/*.md", "docs/**/*")


def _git_changed_files(repo: Path, since: str | None) -> list[str]:
    """返回变更文件相对路径列表。since 为空 → 暂存区；否则 → REF..工作区。"""
    # -c core.quotepath=false: 中文/特殊字符路径不转义，直接输出原始 UTF-8
    cmd = ["git", "-C", str(repo), "-c", "core.quotepath=false", "diff", "--name-only"]
    if since:
        cmd.append(since)
    else:
        cmd.append("--cached")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"❌ git 命令失败: {' '.join(cmd)}\n{r.stderr}", file=sys.stderr)
        sys.exit(2)
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def _is_doc(path: str) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in _DOC_PATTERNS)


def _affected_docs(path: str) -> list[str]:
    """某个代码变更文件 → 受影响文档列表。"""
    hits: list[str] = []
    for glob, docs in DOC_RULES:
        if fnmatch.fnmatch(path, glob):
            hits.extend(docs)
    return hits


def check(repo: Path, since: str | None) -> int:
    changed = _git_changed_files(repo, since)

    code_changes = [f for f in changed if not _is_doc(f) and f]
    doc_changes = set(f for f in changed if _is_doc(f))

    if not code_changes:
        if not since:
            print("✅ 无代码变更，文档同步检查通过")
        return 0

    # 所有代码变更 → 受影响文档全集
    needed: list[str] = []
    for f in code_changes:
        for d in _affected_docs(f):
            if d not in needed:
                needed.append(d)

    if not needed:
        print("✅ 代码变更不影响任何登记文档（或未登记的代码路径），通过")
        return 0

    # needed 里的条目可能是 glob 模式（如 docs/dev/*.md），用它匹配实际变更的文档文件
    unsynced = [d for d in needed if not any(fnmatch.fnmatch(f, d) or f == d for f in doc_changes)]

    if not unsynced:
        print("✅ 相关文档已同步变更")
        return 0

    print("⚠️  代码变更了，但以下相关文档没有同步变更：")
    for d in unsynced:
        print(f"    - {d}")
    print()
    print("本次代码变更文件：")
    for f in code_changes:
        print(f"    - {f}")
    print()
    print("请确认这些文档是否需要更新（新增/改名/行为变化通常需要）。")
    print("若确认无需更新：FP_DOCS_SYNC_ALLOW=1 git commit 显式放行，")

    if os.environ.get("FP_DOCS_SYNC_ALLOW") == "1":
        print("（已通过 FP_DOCS_SYNC_ALLOW=1 放行）")
        return 0
    print("或直接 git commit --no-verify 跳过本钩子。")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(REPO_ROOT), help="仓库根目录")
    parser.add_argument("--since", default=None, help="主动触发：检查 REF..工作区 的变更（如 HEAD~3）")
    args = parser.parse_args()
    return check(Path(args.repo), since=args.since)


if __name__ == "__main__":
    sys.exit(main())
