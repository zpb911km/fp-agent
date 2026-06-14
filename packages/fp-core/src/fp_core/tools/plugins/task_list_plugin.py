"""
Task List 插件 — 列出所有任务（异步版本）

自包含插件，直接读取 tasks.json，不依赖任何全局状态。
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
        "name": "task_list",
        "description": "列出所有任务及其状态",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}


STATUS_LABELS = {
    "pending": "待办",
    "in_progress": "进行中",
    "completed": "已完成",
}


def _sync_list(tasks_path: str) -> str:
    """同步列出任务"""
    if not os.path.exists(tasks_path):
        return "暂无任务"

    try:
        with open(tasks_path, encoding="utf-8") as f:
            data = json.load(f)
            tasks = data.get("tasks", [])
    except Exception as e:
        return f"错误：无法读取任务文件 - {e}"

    if not tasks:
        return "暂无任务"

    lines = ["📋 任务列表:", ""]

    for t in sorted(tasks, key=lambda x: x.get("id", 0)):
        status_en = t.get("status", "unknown")
        status_zh = STATUS_LABELS.get(status_en, status_en)
        lines.append(f"  #{t['id']} [{status_zh:>9}] {t.get('subject', '无标题')}")
        if t.get("description"):
            lines.append(f"       {t['description']}")

    return "\n".join(lines)


async def execute(params: dict[str, Any]) -> str:
    """
    列出所有任务（异步）
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_list, config.TASKS_FILE)
