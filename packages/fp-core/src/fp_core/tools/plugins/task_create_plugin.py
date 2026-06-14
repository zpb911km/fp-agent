"""
Task Create 插件 — 创建新任务（异步版本）

自包含插件，直接读写 tasks.json，不依赖任何全局状态。
"""

import asyncio
import json
import os
from typing import Any

from fp_core import config

# ── 插件定义（OpenAI function calling schema） ──────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "task_create",
        "description": "创建一个任务用于跟踪多步骤工作进度",
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "任务标题（简短描述）"},
                "description": {"type": "string", "description": "任务详细描述"},
            },
            "required": ["subject", "description"],
        },
    },
}


def _sync_create(subject: str, description: str, tasks_path: str) -> str:
    """同步创建任务"""
    os.makedirs(os.path.dirname(tasks_path), exist_ok=True)

    if os.path.exists(tasks_path):
        try:
            with open(tasks_path, encoding="utf-8") as f:
                data = json.load(f)
                tasks = data.get("tasks", [])
                next_id = data.get("next_id", 1)
        except Exception:
            tasks, next_id = [], 1
    else:
        tasks, next_id = [], 1

    new_task = {
        "id": next_id,
        "subject": subject,
        "description": description,
        "status": "pending",
    }
    tasks.append(new_task)

    with open(tasks_path, "w", encoding="utf-8") as f:
        json.dump({"tasks": tasks, "next_id": next_id + 1}, f, ensure_ascii=False, indent=2)

    return f"✅ 已创建任务 #{new_task['id']}: {subject}"


async def execute(params: dict[str, Any]) -> str:
    """
    创建新任务（异步）
    """
    subject = params.get("subject", "")
    description = params.get("description", "")

    if not subject or not description:
        raise ValueError("task_create 需要 subject 和 description 参数")

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_create, subject, description, config.TASKS_FILE)
