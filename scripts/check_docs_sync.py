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

    反向关联（DOC_LINKS，文档 → 代码文件）：
      - 文档变更时：若其关联代码本次未同步变更 → 输出提醒（不阻断，文档允许先行）
      - --audit：校验文档登记的关联代码文件仍存在于仓库（代码重构/改名后
        文档里「源码真相」引用断裂时能被发现）

    这样新增/改名/重组都不怕：不需要预知任何名称，跟着 diff 走即可。

    在此之上还有两档「常驻检查」（每次运行都执行，不依赖 diff）：
      1. 生成物同步（SYNC_PAIRS）：docs/packages/*.md 是唯一权威主文档，
         packages/*/README.md 是它的生成物（PyPI 落地页）。两者不一致 →
         提醒运行 --sync（阻断提交）；生成物被手工改动也会被点名（改主文档）。
      2. 游离文档扫描（STRAY）：全仓库 *.md 中既不在 docs/ 树、也不是登记的
         生成物、又不在白名单（GitHub 模板 / SECURITY / CODE_OF_CONDUCT /
         提示词资产 / .fp 记忆）→ 提醒归档进 docs/ 或登记。

用法:
    python scripts/check_docs_sync.py                  # pre-commit：检查暂存区
    python scripts/check_docs_sync.py --since HEAD~3   # 主动触发：最近 3 次提交
    python scripts/check_docs_sync.py --sync           # 同步：主文档 → packages/*/README.md
    python scripts/check_docs_sync.py --audit          # 审计：未覆盖代码 + 关联断裂 + 游离文档
    python scripts/check_docs_sync.py --repo PATH      # 指定仓库根（测试用）

环境变量:
    FP_DOCS_SYNC_ALLOW=1  显式放行：跳过阻断（exit 0），仅输出提醒

退出码:
    0  无代码变更 / 关联文档已同步 / 显式放行 / 审计无盲区
    1  存在「代码变了但关联文档没变」的提醒（提交场景将阻断）
       （--audit 模式下 = 存在未被规则覆盖的代码文件，或文档关联代码文件已不存在）
"""

import argparse
import fnmatch
import os
import re
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
        [
            "docs/guide/命令参考.md",
            "docs/self/扩展命令.md",
            "README.md",
            "docs/packages/fp.md",
            "docs/packages/fp-terminal.md",
        ],
    ),
    # 工具（含 extensions 子目录）
    (
        "packages/fp-core/src/fp_core/tools/*.py",
        ["docs/dev/工具系统.md"],
        ["docs/guide/工具概览.md", "docs/self/扩展工具.md", "README.md", "docs/packages/fp-core.md"],
    ),
    # 生命周期钩子
    (
        "packages/fp-core/src/fp_core/core/lifecycle.py",
        ["docs/dev/插件系统.md"],
        ["docs/guide/插件系统.md", "docs/self/扩展插件.md", "README.md", "docs/packages/fp-core.md"],
    ),
    # TUI 交互层：键位/面板行为一变，两处用户文档（README 表格 + CLI 入门）必然漂移
    (
        "packages/fp-terminal/src/fp_cli/tui*.py",
        ["docs/dev/显示层.md"],
        ["docs/guide/CLI入门.md", "docs/packages/fp-terminal.md"],
    ),
    (
        "packages/fp-terminal/src/fp_cli/keys_panel.py",
        ["docs/dev/显示层.md"],
        ["docs/guide/CLI入门.md"],
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
    # Zeta 邻居协议（子目录插件：上面的 plugins/*.py 只匹配一层，子目录须单列）
    (
        "packages/fp-core/src/fp_core/plugins/zeta/*.py",
        ["docs/dev/zeta_协议.md"],
        [],
    ),
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
    # FP 首跑配置门禁/向导（入口层，行为文档随项目概览；新增/删除即结构变更）
    (
        "packages/fp/src/fp/setup.py",
        ["docs/dev/项目概览.md"],
        ["docs/guide/快速开始.md", "docs/guide/配置指南.md"],
    ),
    ("packages/fp/src/fp/docs.py", ["docs/dev/项目概览.md"], ["docs/guide/CLI入门.md", "docs/self/README.md"]),
    ("packages/fp/src/fp/version_checker.py", ["docs/dev/项目概览.md"], []),
    # fp ext 扩展分发系统（新增模块）
    ("packages/fp/src/fp/ext*.py", ["docs/dev/资产分发系统.md", "docs/dev/项目概览.md"], ["docs/self/扩展工具.md"]),
    # 终端界面（src/ 一条即覆盖子目录；build/ 不匹配 src 前缀）
    ("packages/fp-terminal/src/*.py", ["docs/dev/显示层.md"], ["docs/guide/CLI入门.md"]),
    # WebUI
    ("packages/fp-webui/src/*.py", ["docs/guide/WebUI手册.md", "docs/packages/fp-webui.md"], []),
    # ACP 协议
    ("packages/fp-acp/src/*.py", ["docs/acp/README.md", "docs/packages/fp-acp.md"], []),
    # ACP 测试（wire 全链路 / MCP 会话隔离）——此前未登记，属"门禁不设防"的盲区
    ("packages/fp-acp/tests/*.py", ["docs/dev/测试.md"], ["docs/dev/测试.md"]),
    # 测试（含 conftest）— 变更需确认测试文档
    # 结构变更（新增测试文件）也提醒测试文档：覆盖范围表通常要同步更新
    ("packages/fp-core/tests/*.py", ["docs/dev/测试.md"], ["docs/dev/测试.md"]),
    # 仓库根 conftest（测试环境引导：LLM key / git 身份兜底，见测试.md「准备」）
    ("conftest.py", ["docs/dev/测试.md"], []),
    # fp 包测试（fp ext 分发系统等）
    ("packages/fp/tests/*.py", ["docs/dev/测试.md"], ["docs/dev/测试.md"]),
    # 终端测试（ask 解析契约等）
    ("packages/fp-terminal/tests/*.py", ["docs/dev/测试.md"], ["docs/dev/测试.md"]),
    # WebUI 桥/API 测试（WS 事件语义、上下文改写连续性校验；新增测试文件需同步一览表）
    ("packages/fp-webui/tests/*.py", ["docs/dev/测试.md"], ["docs/dev/测试.md"]),
    # WebUI 前端静态资源（ask 卡片等组件行为随 WebUI 手册走）
    ("packages/fp-webui/src/fp_webui/static/*.js", ["docs/guide/WebUI手册.md"], []),
    ("packages/fp-webui/src/fp_webui/static/*.css", ["docs/guide/WebUI手册.md"], []),
    ("packages/fp-webui/src/fp_webui/static/*.html", ["docs/guide/WebUI手册.md"], []),
    # 开发/脚本工具
    ("scripts/*.py", ["docs/CONTRIBUTING.md"], []),
    # 包内开发脚本（如 fp-webui 前端依赖图生成）
    ("packages/*/scripts/*.py", ["docs/CONTRIBUTING.md"], []),
    # 工程配置
    (".pre-commit-config.yaml", ["docs/CONTRIBUTING.md"], []),
    # CI 工作流（矩阵 / 平台覆盖变化 → 测试文档同步；结构变更另确认贡献指南）
    (".github/workflows/*.yml", ["docs/dev/测试.md"], ["docs/CONTRIBUTING.md"]),
    ("pyproject.toml", ["docs/dev/测试.md"], []),
    # 包级 pyproject：pytest.pythonpath / 测试依赖 / 构建元数据都会影响「怎么跑测试」
    # （曾漏登记 → `packages/fp-core/pyproject.toml` 加 pythonpath 时门禁静默放行）
    ("packages/*/pyproject.toml", ["docs/dev/测试.md"], []),
]

# ── 反向关联：文档 → 关联代码文件（相对仓库根） ────────────────────────
# 每条规则: (doc_glob, [code_glob, ...])  均为 fnmatch glob
#   doc_glob    文档路径（fnmatch 的 `*` 跨 `/`，可用 docs/* 覆盖整树）
#   code_glob   该文档「源码真相」所指向的代码文件
# 用途：
#   1) --audit：校验关联代码文件仍存在于仓库（代码重构/改名后链接断裂能被发现）
#   2) 文档变更时：若关联代码本次未同步变更 → 提醒（不阻断，文档允许先行）
# 登记原则：只登记「文档内容明确指向实现」的文档（self 操作指南、dev 行为文档），
#           不要为纯清单/索引类文档（README）登记，避免噪音。
DOC_LINKS: list[tuple[str, list[str]]] = [
    # fp ext 扩展分发系统：self 操作指南 ↔ ext*.py 实现模块
    (
        "docs/self/扩展分发.md",
        [
            "packages/fp/src/fp/ext.py",
            "packages/fp/src/fp/ext_assets.py",
            "packages/fp/src/fp/ext_store.py",
            "packages/fp/src/fp/ext_scanner.py",
            "packages/fp/src/fp/ext_manifest.py",
            "packages/fp/src/fp/ext_migrate.py",
            "packages/fp/src/fp/ext_git.py",
        ],
    ),
    # dev 设计文档 ↔ ext 模块（用 glob 覆盖全部 ext* 模块，新模块自动纳入）
    ("docs/dev/资产分发系统.md", ["packages/fp/src/fp/ext*.py"]),
    # Zeta 协议：唯一权威文档 ↔ 插件全部模块
    ("docs/dev/zeta_协议.md", ["packages/fp-core/src/fp_core/plugins/zeta/*.py"]),
]

# ── 生成物同步：主文档（docs/）→ 包内 README（生成物） ──────────────────
# 每条: (主文档, 生成物) —— 均相对仓库根。
# 为什么不是软链/硬链：见 docs/dev/资产分发系统.md §4.2 —— git 只存链接本身，
# 别人 clone 到的是断链、发布物里没有内容；且 Windows CI（ci.yml windows-latest）
# 默认把软链 checkout 成文本文件。故用「脚本生成 + 门禁比对」，与 release.yml
# 注入 docs/ 的做法同一套心智：docs/ 是唯一真相，包内文件是分发产物。
SYNC_PAIRS: list[tuple[str, str]] = [
    ("docs/packages/fp.md", "packages/fp/README.md"),
    ("docs/packages/fp-core.md", "packages/fp-core/README.md"),
    ("docs/packages/fp-terminal.md", "packages/fp-terminal/README.md"),
    ("docs/packages/fp-webui.md", "packages/fp-webui/README.md"),
    ("docs/packages/fp-acp.md", "packages/fp-acp/README.md"),
]

# 生成物里 docs 内部链接改写用的绝对 URL 前缀（见 _rebase_doc_links）
_DOC_URL_BASE = "https://github.com/zpb911km/fp-agent/blob/main/docs/"

# 游离文档扫描的白名单：这些位置的 .md 有正当归属，不算游离。
#   docs/ 树本身是所有文档的家（整树自动登记，见 _scan_stray_docs）
#   .github/ 是 GitHub 原生模板位置；.fp/ 是 FP 本地记忆/设计稿（非仓库文档）
#   提示词模板是代码资产（随 prompts 规则走，不进 docs 树）
_STRAY_WHITELIST_FILES = {
    "README.md",  # 仓库首页（docs/README.md 的镜像入口，见 docs/README.md 末尾约定）
    "SECURITY.md",  # GitHub 线上仓库安全策略（保留在根部）
    "CODE_OF_CONDUCT.md",  # GitHub 线上仓库行为准则（保留在根部）
}
_STRAY_WHITELIST_PREFIXES = (
    ".fp/",
    ".github/",
    "packages/fp-core/src/fp_core/prompts/",
)

# 文档文件判定（见 _is_doc）：所有 Markdown 都算文档——变更它不构成「代码变更」。
# 例外：src/ 下的提示词模板（如 prompts/agent.md）是行为资产，按代码对待，
# 其变更走 DOC_RULES 的 prompts 规则提醒 docs/dev/引擎.md。

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
    """是否文档（文档变更 ≠ 代码变更）。

    所有 Markdown 都算文档——包括游离文档与生成物。例外：src/ 下的提示词模板
    是行为资产，按代码对待（否则改提示词不会提醒引擎文档）。
    """
    if not path.endswith(".md"):
        return False
    return "/src/" not in f"/{path}"


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


def _linked_code_globs(doc: str) -> list[str]:
    """给定文档路径 → 关联代码 glob 列表（DOC_LINKS，first-match-wins）。"""
    for doc_glob, code_globs in DOC_LINKS:
        if fnmatch.fnmatch(doc, doc_glob):
            return list(code_globs)
    return []


def _audit_broken_doc_links(repo: Path) -> list[str]:
    """扫描 DOC_LINKS：关联代码文件在仓库中不存在 → 链接断裂清单。

    返回格式: "doc_glob -> code_glob"（代码被重构/改名后文档里的「源码真相」失联）。
    """
    broken: list[str] = []
    for doc_glob, code_globs in DOC_LINKS:
        for cg in code_globs:
            if not list(repo.glob(cg)):
                broken.append(f"{doc_glob} -> {cg}")
    return sorted(broken)


# ── 生成物同步（docs/packages/*.md → packages/*/README.md） ────────────


def _generated_copies() -> dict[str, str]:
    """{生成物路径: 主文档路径}"""
    return {copy: master for master, copy in SYNC_PAIRS}


def _rebase_doc_links(text: str) -> str:
    """主文档里的 docs 内部相对链接 → 仓库绝对 URL。

    主文档位于 docs/packages/，`../guide/x.md` 指向 docs/guide/x.md；
    生成物位于 packages/<pkg>/README.md，同样写法会指向不存在的
    packages/guide/x.md。故生成时改写为 GitHub 绝对 URL（PyPI 上可点）。
    `../../LICENSE` 这类指向仓库根的链接在两个位置解析一致，不改写。
    """
    return re.sub(
        r"\]\(\.\./(?!\.)([^)]*?\.md(?:#[^)]*)?)\)",
        lambda m: f"]({_DOC_URL_BASE}{m.group(1)})",
        text,
    )


def _render_copy(master_text: str, master_rel: str) -> str:
    """主文档内容 → 生成物文本（顶部加不可见的「勿手改」横幅）。

    横幅用 HTML 注释：PyPI/GitHub 渲染时不可见，但打开文件的人一眼能看到。
    """
    body = master_text if master_text.endswith("\n") else master_text + "\n"
    banner = (
        f"<!-- ⚠️ 生成物：本文件由 scripts/check_docs_sync.py --sync 从 {master_rel} 生成。\n"
        f"     请修改主文档 {master_rel}，不要直接编辑本文件。 -->\n\n"
    )
    return banner + _rebase_doc_links(body)


def _sync_status(repo: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """(漂移的 (主文档, 生成物) 列表, 缺失的主文档列表)。"""
    drift: list[tuple[str, str]] = []
    missing: list[str] = []
    for master_rel, copy_rel in SYNC_PAIRS:
        master = repo / master_rel
        if not master.exists():
            missing.append(master_rel)
            continue
        expected = _render_copy(master.read_text(encoding="utf-8"), master_rel)
        copy = repo / copy_rel
        actual = copy.read_text(encoding="utf-8") if copy.exists() else ""
        if actual != expected:
            drift.append((master_rel, copy_rel))
    return drift, missing


def _sync_apply(repo: Path) -> int:
    """--sync：把主文档渲染写入生成物。返回更新的文件数。"""
    updated = 0
    for master_rel, copy_rel in SYNC_PAIRS:
        master = repo / master_rel
        copy = repo / copy_rel
        if not master.exists():
            print(f"❌ 主文档不存在，跳过: {master_rel}")
            continue
        expected = _render_copy(master.read_text(encoding="utf-8"), master_rel)
        actual = copy.read_text(encoding="utf-8") if copy.exists() else None
        if actual == expected:
            print(f"  = {copy_rel}")
            continue
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text(expected, encoding="utf-8")
        print(f"  ✏️  {copy_rel}  ←  {master_rel}")
        updated += 1
    print(f"✅ 同步完成（{updated} 个文件更新）")
    return 0


# ── 游离文档扫描 ──────────────────────────────────────────────────────


def _scan_stray_docs(repo: Path) -> list[str]:
    """全仓库 *.md 中脱离了统一文档管理的文件（相对路径，已排序）。

    不入册 = 不在 docs/ 树、不是登记的生成物、不在白名单 → 视为游离。
    """
    registered = set(_generated_copies())
    stray: list[str] = []
    for p in repo.rglob("*.md"):
        rel = p.relative_to(repo).as_posix()
        parts = p.parts
        if any(part in _AUDIT_IGNORED_DIRS or part.endswith(".egg-info") for part in parts):
            continue
        if rel.startswith("docs/"):
            continue  # docs/ 是文档的家，整树自动登记
        if rel in registered or rel in _STRAY_WHITELIST_FILES:
            continue
        if any(rel.startswith(pfx) for pfx in _STRAY_WHITELIST_PREFIXES):
            continue
        stray.append(rel)
    return sorted(stray)


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

    # ── 常驻检查 1：生成物 ↔ 主文档 漂移 ──────────────────────────────
    drift, missing = _sync_status(repo)
    gen_copies = _generated_copies()
    allow = os.environ.get("FP_DOCS_SYNC_ALLOW") == "1"

    if missing:
        print("❌ 登记的主文档不存在（生成物无从同步）：")
        for m in missing:
            print(f"    - {m}")
        print("    修复：先恢复主文档，或从 SYNC_PAIRS 移除该登记。")
        print()
    if drift:
        print("🔄 生成物与主文档不同步（packages/*/README.md 由 docs/packages/*.md 生成）：")
        for master, copy in drift:
            print(f"    - {copy}  ≠  {master}")
        print("    修复：python scripts/check_docs_sync.py --sync")
        print()

    # 生成物被手工改动：改了生成物而没改主文档 → 点名（改动会被下次 --sync 覆盖）
    # 判据需排除「刚由 --sync 生成」的情形：生成物在变更集里且与主文档一致 = 正常同步结果。
    drifted_copies = {copy for _, copy in drift}
    changed_masters = {master for master, _ in drift if master in doc_changes}
    manual_edits = [
        f for _, f in changed if f in gen_copies and f in drifted_copies and gen_copies[f] not in changed_masters
    ]
    if manual_edits:
        print("⚠️  以下生成物被手工改动（应改主文档，产物会被 --sync 覆盖）：")
        for f in manual_edits:
            print(f"    - {f} ← 请改 {gen_copies[f]}")
        print()

    # ── 常驻检查 2：游离文档扫描（警告，不阻断） ──────────────────────
    stray = _scan_stray_docs(repo)
    if stray:
        print("📄 游离文档（未纳入 docs/ 统一管理，也无登记）：")
        for f in stray:
            print(f"    - {f}")
        print("    处理：归档进 docs/（推荐）／登记进 SYNC_PAIRS 或白名单／确认为过程产物删除。")
        print()

    # 反向提醒：文档变更 + 关联代码未同步变更（不阻断，文档允许先行编写/补充说明）
    reverse_hits: list[tuple[str, str]] = []
    for doc in sorted(doc_changes):
        for cg in _linked_code_globs(doc):
            if not any(fnmatch.fnmatch(c, cg) for _, c in code_changes):
                reverse_hits.append((doc, cg))

    if reverse_hits:
        print("📄 文档变更，但以下关联代码本次未同步变更（请确认描述与实现一致，不阻断）：")
        for doc, cg in reverse_hits:
            print(f"    - {doc} → {cg}")
        print()

    if not code_changes:
        if drift:
            if allow:
                print("（已通过 FP_DOCS_SYNC_ALLOW=1 放行生成物漂移）")
                return 0
            print("❌ 生成物与主文档不同步，提交被阻断。")
            print("   修复：python scripts/check_docs_sync.py --sync")
            return 1
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
        if drift:
            if allow:
                print("（已通过 FP_DOCS_SYNC_ALLOW=1 放行生成物漂移）")
                return 0
            print("❌ 生成物与主文档不同步（见上），提交被阻断。")
            print("   修复：python scripts/check_docs_sync.py --sync")
            return 1
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
    parser.add_argument(
        "--sync", action="store_true", help="同步模式：把 docs/packages/*.md 生成到 packages/*/README.md"
    )
    parser.add_argument("--audit", action="store_true", help="审计模式：未覆盖代码文件 + 文档关联断裂 + 游离文档")
    args = parser.parse_args()

    if args.sync:
        return _sync_apply(Path(args.repo))

    if args.audit:
        repo = Path(args.repo)
        unmapped = _audit_unmapped_code_files(repo)
        broken = _audit_broken_doc_links(repo)
        stray = _scan_stray_docs(repo)
        drift, missing = _sync_status(repo)
        problems = 0
        if unmapped:
            problems += 1
            print("⚠️  以下代码文件未被任何文档规则覆盖：")
            for f in unmapped:
                print(f"    - {f}")
            print()
            print("建议：在 scripts/check_docs_sync.py 的 DOC_RULES 中登记它们，")
            print("      否则这些文件的变更不会触发任何文档提醒。")
            print()
        if broken:
            problems += 1
            print("⚠️  以下文档登记的关联代码文件在仓库中不存在（「源码真相」链接可能已因重构/改名断裂）：")
            for b in broken:
                print(f"    - {b}")
            print()
            print("建议：更新 scripts/check_docs_sync.py 的 DOC_LINKS，")
            print("      将文档指向当前实际存在的代码文件。")
            print()
        if drift or missing:
            problems += 1
            if missing:
                print("⚠️  以下登记的主文档不存在：")
                for m in missing:
                    print(f"    - {m}")
                print()
            if drift:
                print("⚠️  以下生成物与主文档不同步：")
                for master, copy in drift:
                    print(f"    - {copy}  ≠  {master}")
                print("    修复：python scripts/check_docs_sync.py --sync")
                print()
        if stray:
            problems += 1
            print("⚠️  以下 .md 游离在统一文档管理之外（既不在 docs/ 树，也非登记生成物/白名单）：")
            for f in stray:
                print(f"    - {f}")
            print()
            print("建议：归档进 docs/（推荐），或登记进 SYNC_PAIRS/白名单，")
            print("      或确认为一次性过程产物后删除。")
            print()
        if problems:
            return 1
        print("✅ 代码文件全覆盖、文档关联完好、生成物同步、无游离文档，无盲区")
        return 0

    return check(Path(args.repo), since=args.since)


if __name__ == "__main__":
    sys.exit(main())
