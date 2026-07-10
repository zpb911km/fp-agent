"""
Memory Read 插件 v2 — 读取/搜索长期记忆（异步版本）

四种入口：
  ① memory_read()                          → 顶层索引（两棵树 + 使用引导）
  ② memory_read(name="subagent")           → 精确读取一条（本地优先）
  ③ memory_read(path="~/skill")            → 浏览分类
  ④ memory_read(query="子代理")            → 全文搜索

两棵根树：
  ~/  全局（~/.local/share/fp/memory/）
  ./  本地（$CWD/.fp/memory/）
"""

import asyncio
import os
import re
from typing import Any

from fp_core import config

# ── 插件定义（OpenAI function calling schema） ──────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "memory_read",
        "description": "读取已保存的长期记忆（跨会话）。空查询时列出全部，否则按关键词搜索。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "精确读取一条记忆（从当前可见上下文中取）。同名冲突时本地优先。",
                },
                "path": {
                    "type": "string",
                    "description": "浏览分类，如'~/skill'或'./project'。path 以 ~/ 或 ./ 开头表示根。",
                },
                "query": {
                    "type": "string",
                    "description": "全文搜索（空格分隔，AND 匹配），搜索 name/description/type/content",
                },
            },
            "description": "四种用法：①空参→索引 ②name='foo'→精确 ③path='~/skill'→浏览 ④query='关键词'→搜索",
        },
    },
}


def _parse_frontmatter(content: str) -> dict:
    """解析 YAML frontmatter（第一对 --- 之间）。

    优先用 yaml.safe_load（正确解析多行/引号/特殊字符），
    失败时降级为行扫描（兼容含有二进制字符的旧文件）。
    """
    import yaml

    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    end = 1
    while end < len(lines) and lines[end].strip() != "---":
        end += 1
    if end >= len(lines):
        return {}
    fm_text = "\n".join(lines[1:end])
    try:
        fm = yaml.safe_load(fm_text)
        if isinstance(fm, dict):
            return fm
    except Exception:
        pass
    # 降级：行扫描
    result = {}
    for line in fm_text.split("\n"):
        if line.startswith("name:"):
            result["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("type:"):
            result["type"] = line.split(":", 1)[1].strip()
        elif line.startswith("description:"):
            result["description"] = line.split(":", 1)[1].strip()
    return result


def _list_memories(memory_dir: str) -> list[dict]:
    """列出目录下所有记忆的元信息"""
    if not os.path.isdir(memory_dir):
        return []

    memories = []
    for fname in sorted(os.listdir(memory_dir)):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(memory_dir, fname)
        name = os.path.splitext(fname)[0]

        try:
            with open(fpath, encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue

        fm = _parse_frontmatter(content)
        mem_type = fm.get("type", "unknown")
        description = fm.get("description", "")

        memories.append({
            "name": name,
            "type": mem_type,
            "description": description,
            "path": fpath,
            "content": content,
            "root": "~" if memory_dir == config.MEMORY_DIR else ".",
        })

    return memories


def _parse_memory_body(content: str) -> str:
    """从文件内容中提取正文（跳过 YAML frontmatter）"""
    lines = content.split("\n")
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body_start = i + 1
                break
    return "\n".join(lines[body_start:]).strip()


def _categorize(memories: list[dict], root_label: str) -> dict[str, list[dict]]:
    """按 type（分类）分组记忆"""
    groups: dict[str, list[dict]] = {}
    for m in memories:
        t = m["type"] or "uncategorized"
        groups.setdefault(t, []).append(m)
    return groups


def _build_tree_index(global_memories: list[dict], local_memories: list[dict]) -> str:
    """构建顶层树状索引"""
    lines = ["## 我的长期记忆索引", ""]

    # 全局
    global_groups = _categorize(global_memories, "~")
    global_count = len(global_memories)
    lines.append(f"~/（全局，{global_count}条）")
    for cat in sorted(global_groups):
        names = ", ".join(m["name"] for m in global_groups[cat])
        lines.append(f"  {cat}:    {names}")
    lines.append("")

    # 本地
    local_groups = _categorize(local_memories, ".")
    local_count = len(local_memories)
    if local_count > 0:
        lines.append(f"./（本地 📍 .fp/memory，{local_count}条）")
        for cat in sorted(local_groups):
            names = ", ".join(m["name"] for m in local_groups[cat])
            lines.append(f"  {cat}:    {names}")
        lines.append("")

    # 使用引导
    lines.append("━ 用法 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append('精确读取:   memory_read(name="<记忆名>")       ← 从上方列表中直接使用')
    lines.append('搜索:       memory_read(query="<关键词>")')
    lines.append('浏览分类:   memory_read(path="~/<分类>")       ← 或 memory_read(path="./<分类>")')
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    return "\n".join(lines)


def _build_category_browse(category: str, memories: list[dict], root_label: str) -> str:
    """浏览某个分类下的所有记忆"""
    lines = [f"📂 {root_label}/{category}/（共 {len(memories)} 条）", ""]
    for m in memories:
        lines.append(f"{m['name']:30s} — {m['description']}")
    return "\n".join(lines)


async def execute(params: dict[str, Any]) -> str:
    """
    读取记忆（异步）

    Args:
        params: 可选键 name / path / query

    Returns:
        格式化结果
    """
    name = params.get("name", "").strip()
    path = params.get("path", "").strip()
    query = params.get("query", "").strip()

    loop = asyncio.get_running_loop()

    # 确认目录存在
    os.makedirs(config.MEMORY_DIR, exist_ok=True)

    # 读取两棵树的记忆
    global_memories = await loop.run_in_executor(None, _list_memories, config.MEMORY_DIR)

    local_dir = os.path.join(os.getcwd(), config.MEMORY_DIR_LOCAL)
    local_memories = await loop.run_in_executor(None, _list_memories, local_dir)

    # ── 入口①：空参 → 顶层索引 ──
    if not any([name, path, query]):
        return _build_tree_index(global_memories, local_memories)

    # ── 入口②：memory_read(name="subagent") — 精确读取一条 ──
    if name:
        results = [m for m in global_memories + local_memories if m["name"] == name]

        if not results:
            # 尝试 fuzzy 提示
            hints = [m["name"] for m in global_memories + local_memories]
            close = [h for h in hints if name.lower() in h.lower() or h.lower() in name.lower()]
            msg = f"未找到名为「{name}」的记忆"
            if close:
                msg += f'，相似名称：{", ".join(close[:5])}（使用 memory_read(name="...") 精确读取）'
            return msg

        # 同名冲突时：本地优先（本地排在 global+local 的后面，所以用最后一条）
        selected = results[-1]
        body = _parse_memory_body(selected["content"])
        root_tag = "~" if selected["root"] == "~" else "📍 ./"
        header = f"[{root_tag}/{selected['type']}] {selected['name']} — {selected['description']}"
        return f"📋 {header}\n\n{body}"

    # ── 入口③：memory_read(path="~/skill") — 浏览分类 ──
    if path:
        # 解析 path 语法
        path_match = re.match(r"^(~|\.)/(.+)$", path)
        if not path_match:
            return "path 格式错误。示例：memory_read(path='~/skill') 或 memory_read(path='./project')"

        root_char = path_match.group(1)
        category = path_match.group(2).strip().lower()

        if root_char == "~":
            pool = global_memories
            root_label = "~"
        else:
            pool = local_memories
            root_label = "."

        filtered = [m for m in pool if m["type"].lower() == category]

        if not filtered:
            available = sorted({m["type"] for m in pool})
            msg = f"📂 {root_label}/{category}/ — 该分类下无记忆"
            if available:
                msg += f'\n可选分类：{"、".join(available)}（使用 memory_read(path="{root_label}/<分类>") 浏览）'
            return msg

        return _build_category_browse(category, filtered, root_label)

    # ── 入口④：memory_read(query="子代理") — 全文搜索 ──
    if query:
        keywords = [kw.lower() for kw in query.split()]
        all_memories = global_memories + local_memories

        def matches(m: dict) -> bool:
            search_space = (
                m["name"].lower()
                + " "
                + m["description"].lower()
                + " "
                + m["type"].lower()
                + " "
                + _parse_memory_body(m["content"]).lower()
            )
            return all(kw in search_space for kw in keywords)

        results = [m for m in all_memories if matches(m)]

        if not results:
            return f"📋 搜索「{query}」无匹配"

        lines = [f"📋 搜索「{query}」（共 {len(results)} 条）", ""]
        for m in results:
            root_tag = "~" if m["root"] == "~" else "./"
            lines.append(f"{root_tag}{m['type']}/{m['name']} — {m['description']}")
        return "\n".join(lines)

    return "⚠️ 参数异常（不应到达此处）"
