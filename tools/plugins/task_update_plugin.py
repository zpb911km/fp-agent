"""
Task Update 插件 — 更新任务状态（异步版本）

自包含插件，直接读写 tasks.json，不依赖任何全局状态。
"""

import asyncio
import json
import os
from typing import Any, Dict

import config


# ── 插件定义（OpenAI function calling schema） ──────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "task_update",
        "description": "更新已有任务的状态",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "任务 ID"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed"],
                    "description": "新状态：pending=待办, in_progress=进行中，completed=已完成",
                },
            },
            "required": ["task_id", "status"],
        },
    },
}


VALID_STATUSES = ["pending", "in_progress", "completed"]


def _sync_update(task_id: int, status: str, tasks_path: str) -> str:
    """同步更新任务状态"""
    if not os.path.exists(tasks_path):
        return "错误：未找到任务文件"
    
    try:
        with open(tasks_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            tasks = data.get("tasks", [])
            next_id = data.get("next_id", 1)
    except Exception as e:
        return f"错误：无法读取任务文件 - {e}"
    
    for t in tasks:
        if t["id"] == task_id:
            old_status = t["status"]
            t["status"] = status
            with open(tasks_path, "w", encoding="utf-8") as f:
                json.dump({"tasks": tasks, "next_id": next_id}, f, ensure_ascii=False, indent=2)
            return f"✅ 任务 #{task_id} 状态已从 [{old_status}] 更新为 [{status}]"
    
    return f"错误：未找到任务 #{task_id}"


async def execute(params: Dict[str, Any]) -> str:
    """
    更新任务状态（异步）
    """
    task_id = params.get("task_id")
    status = params.get("status")
    
    if task_id is None or status is None:
        raise ValueError("task_update 需要 task_id 和 status 参数")
    
    if status not in VALID_STATUSES:
        raise ValueError(f"status 必须是 {VALID_STATUSES} 之一")
    
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_update, task_id, status, config.TASKS_FILE)
