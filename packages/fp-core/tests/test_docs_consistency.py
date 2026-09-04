"""文档一致性测试 — 防止「文档漂移」

背景：README / docs 中的数字声明（钩子数、命令数、工具数）曾多次与源码脱节
（23 vs 15 钩子、12/13/16 vs 15 命令、15+ vs 14 工具），且无人察觉。

本测试将「源码事实」作为权威：
    - 钩子数   = fp_core/core/lifecycle.py 中 LifecycleHook 枚举成员数
    - 命令数   = fp_core/commands/ 下命令文件数
    - 核心工具 = fp_core/tools/core.py 中 CORE_TOOLS 数
    - 插件工具 = tools/extensions/ + 生命周期插件动态注册数

然后扫描 README.md 与 docs/**/*.md，凡出现「N 个钩子/命令/工具」类声明，
必须与权威值一致。任何增删钩子/命令/工具后文档未同步，本测试即失败。

规则：
    - 关键词匹配采用「长词优先」：生命周期钩子 > 钩子点 > 钩子；
      内置命令 > 斜杠命令 > 命令；核心工具 > 插件工具 > 工具
    - 「斜杠命令」允许等于命令总数或总数-1（`/exit!` 名字带 `!` 的特例）
    - docs/CHANGELOG.md 为历史日志，跳过
"""

import ast
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]  # packages/fp-core/tests → 仓库根
SRC = REPO_ROOT / "packages" / "fp-core" / "src" / "fp_core"

# ── 权威事实源（从源码计算，不 import，避免环境依赖） ──────────────


def _hook_count() -> int:
    src = (SRC / "core" / "lifecycle.py").read_text(encoding="utf-8")
    m = re.search(r"class LifecycleHook\(Enum\):(.*?)(?=\n\n#|class )", src, re.S)
    assert m, "未找到 LifecycleHook 枚举定义"
    return len(re.findall(r"^    ([A-Z_]+) = auto\(\)", m.group(1), re.M))


def _plugin_injected_commands() -> set[str]:
    """生命周期插件通过 register_command 动态注入的命令名（不走自动发现）"""
    names: set[str] = set()
    for f in (SRC / "plugins").rglob("*.py"):
        if "__pycache__" in str(f):
            continue
        src = f.read_text(encoding="utf-8")
        names.update(re.findall(r'register_command\(\s*"([^"]+)"', src))
    return names


def _command_count() -> int:
    """命令总数 = commands/ 自动发现文件数 + 插件注入命令数"""
    d = SRC / "commands"
    files = len([f for f in os.listdir(d) if f.endswith(".py") and f != "__init__.py"])
    return files + len(_plugin_injected_commands())


def _core_tool_count() -> int:
    src = (SRC / "tools" / "core.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        # 支持 `CORE_TOOLS = [...]`（Assign）与 `CORE_TOOLS: list = [...]`（AnnAssign）
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = getattr(node, "target", None)
            is_core_tools = isinstance(target, ast.Name) and target.id == "CORE_TOOLS"
            if is_core_tools and isinstance(node.value, (ast.List, ast.Tuple)):
                return len(node.value.elts)
    raise AssertionError("未找到 CORE_TOOLS 定义")


def _extension_tool_count() -> int:
    d = SRC / "tools" / "extensions"
    return len([f for f in os.listdir(d) if f.endswith(".py") and f != "__init__.py"])


def _plugin_registered_tool_count() -> int:
    """生命周期插件在 ON_INIT 动态注册的工具数（task_system + shortcircuit）"""
    total = 0
    # task_system：tools.py 中 function.name
    ts = (SRC / "plugins" / "task_system" / "tools.py").read_text(encoding="utf-8")
    total += len(re.findall(r'"name"\s*:\s*"([^"]+)"', ts))
    # shortcircuit：register_tool("<name>", ...)（负向后顾排除 unregister_tool，
    # 否则 on_unregister 中的成对清理会被误计为注册）
    sc = (SRC / "plugins" / "shortcircuit" / "plugin.py").read_text(encoding="utf-8")
    total += len(re.findall(r'(?<!un)register_tool\(\s*"([^"]+)"', sc))
    return total


HOOKS = _hook_count()
COMMANDS = _command_count()
CORE_TOOLS = _core_tool_count()
PLUGIN_TOOLS = _extension_tool_count() + _plugin_registered_tool_count()
TOTAL_TOOLS = CORE_TOOLS + PLUGIN_TOOLS

# ── 文档声明 → 权威值映射（长词优先） ─────────────────────────────

# 斜杠命令特例：/exit! 名字带 `!`，文档可能写作「N-1 个斜杠命令 + 1 个特殊命令」
SLASH_COMMAND_ALLOWED = {COMMANDS, COMMANDS - 1}

KEYWORDS = [
    ("生命周期钩子", HOOKS),
    ("钩子点", HOOKS),
    ("钩子", HOOKS),
    ("内置命令", COMMANDS),
    ("斜杠命令", SLASH_COMMAND_ALLOWED),
    ("命令", COMMANDS),
    ("核心工具", CORE_TOOLS),
    ("插件工具", PLUGIN_TOOLS),
    ("工具", TOTAL_TOOLS),
]

# 历史/演进对照等豁免词（该行视为历史描述，不校验）
EXEMPT_WORDS = ("v1", "遗留", "旧架构", "历史")


def _check_text(text: str, path: str, errors: list[str]) -> None:
    for lineno, line in enumerate(text.splitlines(), 1):
        if any(w in line for w in EXEMPT_WORDS):
            continue
        # 匹配「N 个<关键词>」，关键词前允许 markdown 强调符号
        for num, key in _iter_claims(line):
            expected = dict(KEYWORDS)[key]
            if isinstance(expected, (set, tuple)):
                if num not in expected:
                    msg = f"{path}:{lineno}: {key} 声明 {num}，源码实际 {COMMANDS}"
                    errors.append(f"{msg}（{path} 行：{line.strip()}）")
            elif num != expected:
                msg = f"{path}:{lineno}: {key} 声明 {num}，源码实际 {expected}"
                errors.append(f"{msg}（行：{line.strip()}）")


_CLAIM_RE = re.compile(r"(\d+)\s*个\s*\*?[（(]?")


def _iter_claims(line: str):
    """从一行中提取 (数字, 关键词) 对。要求「N 个」后紧跟关键词（允许强调/括号修饰）。"""
    for m in _CLAIM_RE.finditer(line):
        n = int(m.group(1))
        rest = line[m.end() :]
        for key, _ in KEYWORDS:
            if rest.startswith(key):
                yield n, key
                break


def test_hook_command_tool_counts_known():
    """事实源自洽性：让失败信息可读（若权威计算本身有问题，先暴露）"""
    assert HOOKS > 0
    assert COMMANDS > 0
    assert CORE_TOOLS >= 4
    assert PLUGIN_TOOLS >= 5
    assert TOTAL_TOOLS == CORE_TOOLS + PLUGIN_TOOLS


def test_docs_numbers_match_source():
    """README + docs 中所有「N 个钩子/命令/工具」声明与源码一致"""
    md_files = [REPO_ROOT / "README.md"]
    md_files += sorted((REPO_ROOT / "docs").rglob("*.md"))
    md_files = [p for p in md_files if p.name != "CHANGELOG.md"]

    errors: list[str] = []
    for p in md_files:
        _check_text(p.read_text(encoding="utf-8"), str(p.relative_to(REPO_ROOT)), errors)

    assert not errors, "文档漂移！请同步更新文档中的数字声明：\n  " + "\n  ".join(errors[:30])


def test_no_ghost_components_in_docs():
    """文档不应引用源码中不存在的命令/工具名（幽灵组件）"""
    cmd_dir = SRC / "commands"
    real_cmds = {f[:-3] for f in os.listdir(cmd_dir) if f.endswith(".py") and f != "__init__.py"}
    # 插件注入命令（如 shortcircuit 插件注册的 sc）也是合法触发词
    real_cmds.update(_plugin_injected_commands())
    # name 可能与文件名不同（如 shortcircuit.py 的 name="sc"）；aliases 也是合法触发词
    for f in os.listdir(cmd_dir):
        if not f.endswith(".py") or f == "__init__.py":
            continue
        src = (cmd_dir / f).read_text(encoding="utf-8")
        m = re.search(r'^name\s*=\s*"([^"]+)"', src, re.M)
        if m:
            real_cmds.add(m.group(1))
        a = re.search(r"^aliases\s*(?::[^=]*)?=\s*\[(.*?)\]", src, re.M | re.S)
        if a:
            real_cmds.update(re.findall(r'"([^"]+)"', a.group(1)))

    # 工具名：core + extensions + 插件注册
    real_tools = set()
    core_src = (SRC / "tools" / "core.py").read_text(encoding="utf-8")
    real_tools.update(re.findall(r'name\s*=\s*"([a-z_]+)",', core_src))
    for f in os.listdir(SRC / "tools" / "extensions"):
        if not f.endswith(".py") or f == "__init__.py":
            continue
        src = (SRC / "tools" / "extensions" / f).read_text(encoding="utf-8")
        real_tools.update(re.findall(r'"name"\s*:\s*"([^"]+)"', src))
    ts = (SRC / "plugins" / "task_system" / "tools.py").read_text(encoding="utf-8")
    real_tools.update(re.findall(r'"name"\s*:\s*"([^"]+)"', ts))
    sc = (SRC / "plugins" / "shortcircuit" / "plugin.py").read_text(encoding="utf-8")
    real_tools.update(re.findall(r'register_tool\(\s*"([^"]+)"', sc))

    # 段落级豁免：标题含这些词的章节视为「部署环境/示例」描述，不校验内置性
    section_skip_words = ("用户级", "示例", "非仓库内置", "部署环境")
    # 行级豁免：API 端点路径 / 占位符 / 部署环境引用
    api_context_words = (
        "端点",
        "API",
        "GET ",
        "POST ",
        "DELETE ",
        "PUT ",
        "/api/",
        "用户级",
        "用户目录",
        "部署环境",
        "非仓库内置",
    )

    suspicious = []
    for p in [REPO_ROOT / "README.md"] + sorted((REPO_ROOT / "docs").rglob("*.md")):
        if p.name == "CHANGELOG.md":
            continue
        in_skip_section = False
        for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                in_skip_section = any(w in line for w in section_skip_words)
                continue
            if in_skip_section:
                continue
            if any(w in line for w in api_context_words):
                continue
            ghost_re = (
                r"`(/[a-z_!?]+|web_search|file_fingerprint|delete_empty_sessions|"
                r"kdeconnect\w*|vision|codegraph\w*|ask_llm|smart_web_search)`"
            )
            for name in re.findall(ghost_re, line):
                clean = name.lstrip("/")
                if clean == "xxx":  # 占位符
                    continue
                if clean in real_cmds or clean in real_tools:
                    continue
                suspicious.append(f"{p.relative_to(REPO_ROOT)}:{lineno}: 疑似幽灵组件 `{name}`（源码中不存在）")

    assert not suspicious, "文档引用了源码中不存在的命令/工具：\n  " + "\n  ".join(suspicious[:30])


def test_faq_count_matches_docs():
    """docs/README.md 声明的 FAQ 数量 == FAQ.md 实际问题数"""
    faq = (REPO_ROOT / "docs" / "guide" / "FAQ.md").read_text(encoding="utf-8")
    actual = len(re.findall(r"^## Q\d+:", faq, re.M))
    doc = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    claimed = [int(m.group(1)) for m in re.finditer(r"(\d+)\s*个常见问题", doc)]
    assert claimed, "docs/README.md 中未找到 FAQ 数量声明"
    assert all(n == actual for n in claimed), f"FAQ 数量声明 {claimed} != 实际 {actual}"
