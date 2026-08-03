"""ext manifest — 协议契约解析

两类 manifest：
  ① Python 资产（tools/commands/plugins）→ 文件头部 __fp__ 协议字段
  ② 记忆（memory）→ YAML front-matter（--- name/description/type/created ---）

协议契约（__fp__ schema v1）：
    __fp__ = {
        "name": "asset-name",        # 必填，资产唯一名
        "version": "1.0.0",          # 可选，默认 0.1.0
        "description": "...",        # 可选
        "author": "...",             # 可选
        "license": "MIT",            # 可选
        "type": "tools",             # 可选，tools/commands/plugins/memory（推断时可不填）
        "source": "https://...",     # 可选，来源 URL
    }

解析方式：AST 求值纯字面量（ast.literal_eval），绝不执行代码。
"""

import ast

FP_SCHEMA_VERSION = 1

# ── Python 资产：__fp__ 协议 ─────────────────────────────────────


def parse_fp_manifest(filepath: str) -> dict | None:
    """解析 Python 文件头部的 __fp__ 协议字段（AST 字面量，不执行代码）。

    返回 dict（不保证字段完整，调用方用 validate_manifest 校验）；
    无法解析（非 Python / 语法错误 / 无 __fp__）时返回 None。
    """
    if not filepath.endswith(".py"):
        return None
    try:
        with open(filepath, encoding="utf-8") as f:
            src = f.read()
    except OSError:
        return None
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == "__fp__":
                try:
                    val = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    return None
                if isinstance(val, dict):
                    return val
    return None


# ── 工具语义名：兼容存量 PLUGIN_DEFINITION schema ────────────────

_TOOL_DEF_VARS = ("PLUGIN_DEFINITION", "PLUGIN_DEFS", "TOOLS", "FUNCTIONS")


def parse_tool_name(filepath: str) -> str | None:
    """解析工具语义名：__fp__.name 优先，否则兼容存量 PLUGIN_DEFINITION。

    存量工具（未迁移 __fp__）以 PLUGIN_DEFINITION = {"function": {"name": ...}}
    或 PLUGIN_DEFS = [...] 声明语义名。同样 AST 字面量解析，不执行代码。
    返回 None 表示无法解析（无 __fp__ 也无工具定义）。
    """
    m = parse_fp_manifest(filepath)
    if m and isinstance(m.get("name"), str) and m["name"].strip():
        return m["name"]
    if not filepath.endswith(".py"):
        return None
    try:
        with open(filepath, encoding="utf-8") as f:
            src = f.read()
    except OSError:
        return None
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id in _TOOL_DEF_VARS):
            continue
        try:
            val = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            continue
        defs = val if isinstance(val, list) else [val]
        for d in defs:
            if not isinstance(d, dict):
                continue
            func = d.get("function", d)
            if isinstance(func, dict):
                n = func.get("name")
                if isinstance(n, str) and n.strip():
                    return n
    return None


# ── 记忆：front-matter 解析 ─────────────────────────────────────


def parse_frontmatter(content: str) -> dict:
    """解析记忆文件的 YAML frontmatter（第一对 --- 之间）。

    行扫描实现（memory_save 生成的格式固定为 name/description/type/created
    单行键值，无需完整 YAML；遇复杂内容降级返回空 dict）。
    """
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    end = 1
    while end < len(lines) and lines[end].strip() != "---":
        end += 1
    if end >= len(lines):
        return {}
    result: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key in ("name", "description", "type", "created", "root"):
            result[key] = value.strip()
    return result


def parse_memory_manifest(filepath: str) -> dict | None:
    """解析记忆文件的 manifest（front-matter）。无法解析返回 None。"""
    try:
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None
    fm = parse_frontmatter(content)
    if not fm:
        return None
    return fm


# ── 校验与规范化 ─────────────────────────────────────────────────


def validate_manifest(manifest: dict, asset_type: str | None = None) -> list[str]:
    """校验 manifest，返回问题列表（空列表 = 合法）。

    Args:
        manifest: parse_fp_manifest / parse_memory_manifest 的返回
        asset_type: 期望的资产类型（用于核对 manifest["type"]）
    """
    issues: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest 不是 dict"]
    if not manifest.get("name") or not str(manifest["name"]).strip():
        issues.append("缺少 name（必填）")
    declared_type = manifest.get("type")
    if asset_type and declared_type and declared_type != asset_type:
        issues.append(f"type 不符：manifest 声明 {declared_type!r}，期望 {asset_type!r}")
    return issues
