"""
Memory Save 插件 — 保存长期记忆（异步版本）

用于跨会话持久化用户偏好、项目信息、重要决策等。
"""

import asyncio
import os
from datetime import datetime
from typing import Any

from fp_core import config

# ── 插件定义（OpenAI function calling schema） ──────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "memory_save",
        "description": "保存一条长期记忆（跨会话持久化）。适合记住用户偏好、项目关键信息、重要决策等。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "记忆名称（英文短词，如 user_role, project_goal）"},
                "type": {
                    "type": "string",
                    "enum": ["user", "project", "feedback", "reference"],
                    "description": "记忆类型：user=用户信息，project=项目状态，feedback=反馈偏好，reference=参考资料",
                },
                "description": {"type": "string", "description": "一句话描述，用于检索"},
                "content": {"type": "string", "description": "记忆正文内容"},
            },
            "required": ["name", "type", "description", "content"],
        },
    },
}


VALID_TYPES = ["user", "project", "feedback", "reference"]


async def execute(params: dict[str, Any]) -> str:
    """
    保存记忆（异步）

    Args:
        params: 包含 name, type, description, content 的字典

    Returns:
        保存结果
    """
    name = params.get("name", "")
    mem_type = params.get("type", "")
    description = params.get("description", "")
    content = params.get("content", "")

    # 验证必填参数
    if not all([name, mem_type, description, content]):
        raise ValueError("memory_save 需要以下参数：name, type, description, content")

    # 验证类型
    if mem_type not in VALID_TYPES:
        raise ValueError(f"type 必须是 {VALID_TYPES} 之一")

    memory_dir = config.MEMORY_DIR

    # 安全文件名
    safe_name = name.replace(" ", "_").replace("/", "_")

    path = os.path.join(memory_dir, f"{safe_name}.md")
    date = datetime.now().strftime("%Y-%m-%d %H:%M")

    loop = asyncio.get_running_loop()

    def _write():
        os.makedirs(memory_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("---\n")
            f.write(f"name: {safe_name}\n")
            f.write(f"description: {description}\n")
            f.write(f"type: {mem_type}\n")
            f.write(f"created: {date}\n")
            f.write("---\n\n")
            f.write(content + "\n")

    await loop.run_in_executor(None, _write)

    return f"✅ 记忆已保存：{safe_name} ({mem_type})"
