"""
Memory Read 插件 — 读取/搜索长期记忆（异步版本）

空查询 → 列出所有记忆的摘要
有查询 → 按关键词搜索，精确命中1条时返回完整正文，命中多条时返回摘要
关键词使用空格分隔，AND 逻辑匹配（必须同时包含所有关键词）。
"""

import asyncio
import glob
import os
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
                "query": {
                    "type": "string",
                    "description": "搜索关键词（可选，空传则列出全部；使用空格分隔不同的关键词，AND 逻辑匹配）",
                },
            },
        },
    },
}


def _list_memories(memory_dir: str) -> list[dict]:
    """列出所有记忆的元信息"""
    memories = []
    for path in sorted(glob.glob(os.path.join(memory_dir, "*.md"))):
        name = os.path.splitext(os.path.basename(path))[0]

        with open(path, encoding="utf-8") as f:
            content = f.read()

        description = ""
        mem_type = "unknown"

        for line in content.split("\n"):
            if line.startswith("description:"):
                description = line.split(":", 1)[1].strip()
            elif line.startswith("type:"):
                mem_type = line.split(":", 1)[1].strip()

        memories.append({
            "name": name,
            "type": mem_type,
            "description": description,
            "path": path,
        })

    return memories


def _parse_memory_content(path: str) -> str:
    """从文件中提取正文内容（跳过 YAML frontmatter）"""
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")

    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body_start = i + 1
                break

    return "\n".join(lines[body_start:]).strip()


async def execute(params: dict[str, Any]) -> str:
    """
    读取记忆（异步）

    Args:
        params: 包含可选的 'query' 键的字典

    Returns:
        记忆列表或搜索结果
    """
    query = params.get("query", "").strip()
    memory_dir = config.MEMORY_DIR

    loop = asyncio.get_running_loop()

    # 确保目录存在（同步操作委托到线程池）
    os.makedirs(memory_dir, exist_ok=True)

    # 列出所有记忆
    all_memories = await loop.run_in_executor(None, _list_memories, memory_dir)

    if not all_memories:
        return "暂无记忆"

    # 搜索过滤
    if query:
        keywords = [kw.lower() for kw in query.split()]

        def matches(m: dict) -> bool:
            name_lower = m["name"].lower()
            desc_lower = m["description"].lower()
            return all(kw in name_lower or kw in desc_lower for kw in keywords)

        results = [m for m in all_memories if matches(m)]

        if not results:
            hint = f"未找到匹配「{query}」的记忆，以下是全部可用的记忆："
            lines = [hint, ""]
            for m in all_memories:
                lines.append(f"[{m['type']}] {m['name']} — {m['description']}")
            return "\n".join(lines)

        # ── 精准命中一条 → 返回完整正文 ──
        if len(results) == 1:
            body = await loop.run_in_executor(None, _parse_memory_content, results[0]["path"])
            header = f"[{results[0]['type']}] {results[0]['name']} — {results[0]['description']}"
            return f"📋 {header}\n\n{body}"

        # ── 命中多条 → 返回摘要，提示用户更精确 ──
        memories_to_show = results
    else:
        memories_to_show = all_memories

    # 格式化输出（摘要列表）
    lines = ["📋 记忆列表:", ""]

    for m in memories_to_show:
        lines.append(f"[{m['type']}] {m['name']} — {m['description']}")

    return "\n".join(lines)
