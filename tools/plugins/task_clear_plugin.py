"""
Task Clear 插件 — 清除已完成的任务（异步版本）

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
        "name": "task_clear",
        "description": "清除所有已完成的任务。会删除已完成（completed）状态的记录，并自动更新下个任务 ID。",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}


def _sync_clear(tasks_path: str) -> str:
    """同步执行清除（在 executor 中运行）"""
    if not os.path.exists(tasks_path):
        return "暂无任务文件"
    
    try:
        with open(tasks_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            tasks = data.get("tasks", [])
    except Exception as e:
        return f"错误：无法读取任务文件 - {e}"
    
    completed = [t for t in tasks if t.get("status") == "completed"]
    pending = [t for t in tasks if t.get("status") != "completed"]
    
    if not completed:
        return "没有已完成的任务需要清除"
    
    next_id = (max(t["id"] for t in pending) + 1) if pending else 1
    with open(tasks_path, "w", encoding="utf-8") as f:
        json.dump({"tasks": pending, "next_id": next_id}, f, ensure_ascii=False, indent=2)
    
    return f"✅ 已清除 {len(completed)} 个已完成任务，剩余 {len(pending)} 个待办任务"


async def execute(params: Dict[str, Any]) -> str:
    """
    清除已完成的任务（异步）
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_clear, config.TASKS_FILE)
