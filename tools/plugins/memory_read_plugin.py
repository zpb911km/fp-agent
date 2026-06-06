"""
Memory Read 插件 — 读取/搜索长期记忆

支持列出所有记忆或通过关键词搜索记忆内容。
关键词使用空格分隔，AND 逻辑匹配（必须同时包含所有关键词）。
"""

import glob
import os
from typing import Any, Dict, List

import config


# ── 插件定义（OpenAI function calling schema） ──────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "memory_read",
        "description": "读取已保存的长期记忆（跨会话）。空查询时列出全部，否则按关键词搜索。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词（可选，空传则列出全部；使用空格分隔不同的关键词，AND 逻辑匹配）"},
            },
        },
    },
}


def _list_memories(memory_dir: str) -> List[dict]:
    """列出所有记忆的元信息"""
    memories = []
    for path in sorted(glob.glob(os.path.join(memory_dir, "*.md"))):
        name = os.path.splitext(os.path.basename(path))[0]
        
        with open(path, "r", encoding="utf-8") as f:
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
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().split("\n")
    
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body_start = i + 1
                break
    
    return "\n".join(lines[body_start:]).strip()


def execute(params: Dict[str, Any]) -> str:
    """
    读取记忆
    
    Args:
        params: 包含可选的 'query' 键的字典
        
    Returns:
        记忆列表或搜索结果
    """
    query = params.get("query", "").strip()
    memory_dir = config.MEMORY_DIR
    
    # 确保目录存在
    os.makedirs(memory_dir, exist_ok=True)
    
    # 列出所有记忆
    all_memories = _list_memories(memory_dir)
    
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
            return f"未找到匹配「{query}」的记忆"
        
        memories_to_show = results
    else:
        memories_to_show = all_memories
    
    # 格式化输出
    lines = ["📋 记忆列表:", ""]
    
    for m in memories_to_show:
        lines.append(f"[{m['type']}] {m['name']} — {m['description']}")
        
        # 空查询时显示全部正文内容
        if not query:
            content = _parse_memory_content(m["path"])
            if len(content) > 500:
                content = content[:500] + "  ..."
            lines.append(f"   内容:\n{content}")
            lines.append("")
    
    return "\n".join(lines)
