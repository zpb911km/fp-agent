#!/usr/bin/env python3
"""check_docs_sync.py — 代码变更 → 文档跟进提醒（pre-commit 钩子入口）

机制（以「变更流」为核心，不做文本比对）：
    文档滞后 = 代码发生了会影响文档的变更，而关联文档没有同步变更。

    判定不依赖任何「当前文档对不对」的文本匹配，而是跟着 git diff 走：
      1. 取变更文件清单（提交时=暂存区；主动触发=--since REF）
      2. 代码文件变更 → 通过内置路径规则反查「受影响文档」
      3. 受影响文档在本次变更中没变 → 输出提醒；提交场景默认阻断（exit 1）

    规则按「变更类型」分层（解决「改一行注释也提醒 README」的误报）：
      - 内容修改（M）         → 只要求「行为描述类」文档（docs/dev/…）确认
      - 结构变更（增/删/改名）→ 额外要求「清单类」文档（README/guide 参考/self 扩展）确认

    这样新增/改名/重组都不怕：不需要预知任何名称，跟着 diff 走即可。

用法:
    python scripts/check_docs_sync.py                  # pre-commit：检查暂存区
    python scripts/check_docs_sync.py --since HEAD~3   # 主动触发：最近 3 次提交
    python scripts/check_docs_sync.py --audit          # 审计：列出未被任何规则覆盖的代码文件
    python scripts/check_docs_sync.py --repo PATH      # 指定仓库根（测试用）

环境变量:
    FP_DOCS_SYNC_ALLOW=1  显式放行：跳过阻断（exit 0），仅输出提醒

退出码:
    0  无代码变更 / 关联文档已同步 / 显式放行 / 审计无盲区
    1  存在「代码变了但关联文档没变」的提醒（提交场景将阻断）
       （--audit 模式下 = 存在未被规则覆盖的代码文件）
"""

import argparse
import fnmatch
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── 路径规则：代码 glob → 受影响文档（相对仓库根） ──────────────────────
# 每条规则: (glob, docs_any, docs_structural)
#   glob 用 fnmatch.fnmatch —— 注意 fnmatch 的 `*` 会跨 `/`，因此
#   `src/*.py` 一条即覆盖整个包（含子目录），无需 `**/*.py`。
#   docs_any:        任何变更（含内容修改 M）都要确认的文档（多为 dev 行为文档）
#   docs_structural: 仅结构变更（新增/删除/重命名）才需确认的清单类文档
#                    （README 的数字/清单、guide 参考、self 扩展指南）
# 匹配为 first-match-wins：规则从具体到通用排列，命中即止，避免兜底规则叠加。
DOC_RULES: list[tuple[str, list[str], list[str]]] = [
    # 命令
    (
        "packages/fp-core/src/fp_core/commands/*.py",
        ["docs/dev/命令系统.md"],
        ["docs/guide/命令参考.md", "docs/self/扩展命令.md", "README.md"],
    ),
    # 工具（含 extensions 子目录）
    (
        "packages/fp-core/src/fp_core/tools/*.py",
        ["docs/dev/工具系统.md"],
        ["docs/guide/工具概览.md", "docs/self/扩展工具.md", "README.md"],
    ),
    # 生命周期钩子
    (
        "packages/fp-core/src/fp_core/core/lifecycle.py",
        ["docs/dev/插件系统.md"],
        ["docs/guide/插件系统.md", "docs/self/扩展插件.md", "README.md"],
    ),
    # 核心引擎：按模块精准映射
    ("packages/fp-core/src/fp_core/core/llm_client.py", ["docs/dev/LLM通信.md"], []),
    ("packages/fp-core/src/fp_core/core/llm_service.py", ["docs/dev/LLM通信.md"], []),
    ("packages/fp-core/src/fp_core/core/token_tracker.py", ["docs/dev/LLM通信.md"], []),
    ("packages/fp-core/src/fp_core/core/conversation.py", ["docs/dev/会话模块.md", "docs/guide/会话管理.md"], []),
    ("packages/fp-core/src/fp_core/core/session.py", ["docs/dev/会话模块.md", "docs/guide/会话管理.md"], []),
    ("packages/fp-core/src/fp_core/core/reloader.py", ["docs/dev/自我修改.md"], []),
    ("packages/fp-core/src/fp_core/core/agent.py", ["docs/dev/引擎.md"], []),
    ("packages/fp-core/src/fp_core/core/prompt_builder.py", ["docs/dev/引擎.md"], []),
    ("packages/fp-core/src/fp_core/core/tool_executor.py", ["docs/dev/工具系统.md"], []),
    # core 剩余（io/state/__init__）
    ("packages/fp-core/src/fp_core/core/*.py", ["docs/dev/架构设计.md"], []),
    # 配置
    ("packages/fp-core/src/fp_core/config.py", ["docs/guide/配置指南.md", "docs/dev/配置系统.md"], []),
    # 插件体系
    ("packages/fp-core/src/fp_core/plugins/*.py", ["docs/dev/插件系统.md", "docs/self/扩展插件.md"], []),
    # 提示词（FP 的自我认知）
    ("packages/fp-core/src/fp_core/prompts/*.py", ["docs/dev/引擎.md"], []),
    # 其余 fp_core（logger/platform_utils/__init__ 及未来新模块）兜底
    ("packages/fp-core/src/fp_core/*.py", ["docs/dev/*.md"], []),
    # FP 入口（CLI 分发）
    (
        "packages/fp/src/fp/main.py",
        ["docs/dev/项目概览.md"],
        ["docs/guide/CLI入门.md", "docs/guide/快速开始.md"],
    ),
    ("packages/fp/src/fp/docs.py", ["docs/dev/项目概览.md"], ["docs/guide/CLI入门.md", "docs/self/README.md"]),
    ("packages/fp/src/fp/version_checker.py", ["docs/dev/项目概览.md"], []),
    # 终端界面（src/ 一条即覆盖子目录；build/ 不匹配 src 前缀）
    ("packages/fp-terminal/src/*.py", ["docs/dev/显示层.md"], ["docs/guide/CLI入门.md"]),
    # WebUI
    ("packages/fp-webui/src/*.py", ["docs/guide/WebUI手册.md"], []),
    # ACP 协议
    ("packages/fp-acp/src/*.py", ["docs/acp/README.md"], []),
    # 开发/脚本工具
    ("scripts/*.py", ["docs/CONTRIBUTING.md"], []),
    # 工程配置
    (".pre-commit-config.yaml", ["docs/CONTRIBUTING.md"], []),
]

# 文档文件本身（变更它们不算「代码变更」）
# fnmatch 的 `*` 跨 `/`，`docs/*` 一条即覆盖 docs 全树（含子目录）
_DOC_PATTERNS = ("README.md", "docs/*")

# 结构性变更状态（内容修改 M 之外都算）
_STRUCTURAL_STATUSES = {"A", "D", "C", "T", "R"}


def _git_changed_files(repo: Path, since: str | None) -> list[tuple[str, str]]:
    """返回 (变更状态, 相对路径) 列表。since 为空 → 暂存区；否则 → REF..工作区。

    --no-renames: 重命名拆成 A+D 两行，避免解析 R\told\tnew 的 tab 结构。
    -c core.quotepath=false: 中文/特殊字符路径不转义，直接输出原始 UTF-8。
    """
    cmd = ["git", "-C", str(repo), "-c", "core.quotepath=false", "diff", "--name-status", "--no-renames"]
    if since:
        cmd.append(since)
    else:
        cmd.append("--cached")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"❌ git 命令失败: {' '.join(cmd)}\n{r.stderr}", file=sys.stderr)
        sys.exit(2)
    changes: list[tuple[str, str]] = []
    for ln in r.stdout.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        parts = ln.split("\t", 1)
        status = parts[0][:1]  # A/M/D/C/T
        path = parts[1].strip() if len(parts) > 1 else ""
        if path:
            changes.append((status, path))
    return changes


def _is_doc(path: str) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in _DOC_PATTERNS)


def _is_structural(status: str) -> bool:
    return status in _STRUCTURAL_STATUSES


def _is_mapped_code_file(path: str) -> bool:
    """该代码文件是否被任何 DOC_RULES 覆盖（覆盖 = 变更时会关联文档）"""
    return any(fnmatch.fnmatch(path, glob) for glob, _, _ in DOC_RULES)


# 审计时排除的目录（构建产物 / 缓存 / 依赖 / 测试）
_AUDIT_IGNORED_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".coverage",
    "dist",
    "build",
    ".venv",
    "venv",
    "node_modules",
    ".eggs",
    "*.egg-info",
}


def _audit_unmapped_code_files(repo: Path) -> list[str]:
    """全仓库扫描：未被任何 DOC_RULES 覆盖的代码文件（相对路径）。"""
    unmapped: list[str] = []
    for p in repo.rglob("*.py"):
        parts = p.parts
        # 跳过缓存/构建/依赖目录
        if any(part in _AUDIT_IGNORED_DIRS or part.endswith(".egg-info") for part in parts):
            continue
        rel = p.relative_to(repo).as_posix()
        # 跳过测试文件
        if (
            any(part == "tests" for part in parts)
            or fnmatch.fnmatch(rel, "test_*.py")
            or fnmatch.fnmatch(rel, "*_test.py")
        ):
            continue
        if not _is_mapped_code_file(rel):
            unmapped.append(rel)
    return sorted(unmapped)


def _affected_docs(path: str, structural: bool) -> list[str]:
    """某个代码变更文件 → 受影响文档列表。

    first-match-wins：只取第一条命中的规则（具体在前、兜底在后），
    避免兜底规则叠加导致误报。
    """
    for glob, docs_any, docs_structural in DOC_RULES:
        if fnmatch.fnmatch(path, glob):
            if structural:
                return [*docs_any, *docs_structural]
            return list(docs_any)
    return []


def check(repo: Path, since: str | None) -> int:
    changed = _git_changed_files(repo, since)

    code_changes = [(s, f) for s, f in changed if f and not _is_doc(f)]
    doc_changes = set(f for s, f in changed if f and _is_doc(f))

    if not code_changes:
        if not since:
            print("✅ 无代码变更，文档同步检查通过")
        return 0

    # 未被任何规则覆盖的变更文件 → 提醒（不阻断，这是规则盲区不是滞后）
    unmapped = [f for s, f in code_changes if not _is_mapped_code_file(f)]
    if unmapped:
        print("⚠️  本次变更的代码文件未被任何文档规则覆盖（门禁对它们不设防）：")
        for f in unmapped:
            print(f"    - {f}")
        print("   建议：在 scripts/check_docs_sync.py 的 DOC_RULES 中为这些路径登记关联文档，")
        print("         或确认它们确实无需文档跟进。")
        print()

    # 所有代码变更 → 受影响文档全集
    needed: list[str] = []
    for s, f in code_changes:
        for d in _affected_docs(f, _is_structural(s)):
            if d not in needed:
                needed.append(d)

    if not needed:
        if unmapped:
            print("（本次变更仅涉及未被规则覆盖的文件，无关联文档可检查）")
        else:
            print("✅ 代码变更不影响任何登记文档，通过")
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
    for s, f in code_changes:
        print(f"    - {s}  {f}")
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
    parser.add_argument("--audit", action="store_true", help="审计模式：列出未被任何规则覆盖的代码文件")
    args = parser.parse_args()

    if args.audit:
        unmapped = _audit_unmapped_code_files(Path(args.repo))
        if unmapped:
            print("⚠️  以下代码文件未被任何文档规则覆盖：")
            for f in unmapped:
                print(f"    - {f}")
            print()
            print("建议：在 scripts/check_docs_sync.py 的 DOC_RULES 中登记它们，")
            print("      否则这些文件的变更不会触发任何文档提醒。")
            return 1
        print("✅ 所有代码文件均被文档规则覆盖，无盲区")
        return 0

    return check(Path(args.repo), since=args.since)


if __name__ == "__main__":
    sys.exit(main())
