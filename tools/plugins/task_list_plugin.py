"""
Task List 插件 - 列出所有任务

自包含插件，直接读取 tasks.json，不依赖任何全局状态。
"""

import json
from pathlib import Path
from typing import Dict, Any

from ._plugin_config import get_tasks_file


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


def execute(params: dict) -> str:
    tasks_path = Path(get_tasks_file())
    if not tasks_path.exists():
        return "暂无任务"

    try:
        with open(tasks_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            tasks = data.get("tasks", [])
    except Exception as e:
        return f"错误：无法读取任务文件 - {e}"

    if not tasks:
        return "暂无任务"

    lines = ["📋 任务列表:", ""]
    status_colors = {
        "pending": "待办",
        "in_progress": "进行中",
        "completed": "已完成",
    }

    for t in sorted(tasks, key=lambda x: x.get("id", 0)):
        status_en = t.get("status", "unknown")
        status_zh = status_colors.get(status_en, status_en)
        lines.append(f"  #{t['id']} [{status_zh:>9}] {t.get('subject', '无标题')}")
        if t.get("description"):
            lines.append(f"       {t['description']}")

    return "\n".join(lines)
