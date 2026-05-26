"""
Task Update 插件 - 更新任务状态

自包含插件，直接读写 tasks.json，不依赖任何全局状态。
"""

import json
from pathlib import Path
from typing import Dict, Any

from ._plugin_config import get_tasks_file


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


def execute(params: dict) -> str:
    task_id = params.get("task_id")
    status = params.get("status")
    if task_id is None or status is None:
        raise ValueError("task_update 插件需要 task_id 和 status 参数")

    valid_statuses = ["pending", "in_progress", "completed"]
    if status not in valid_statuses:
        raise ValueError(f"status 必须是 {valid_statuses} 之一")

    tasks_path = Path(get_tasks_file())
    if not tasks_path.exists():
        return "错误：未找到任务文件"

    try:
        with open(tasks_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            tasks = data.get("tasks", [])
            next_id = data.get("next_id", 1)
    except Exception as e:
        return f"错误：无法读取任务文件 - {e}"

    for t in tasks:
        if t["id"] == task_id:
            old_status = t["status"]
            t["status"] = status
            with open(tasks_path, 'w', encoding='utf-8') as f:
                json.dump({"tasks": tasks, "next_id": next_id}, f, ensure_ascii=False, indent=2)
            return f"✅ 任务 #{task_id} 状态已从 [{old_status}] 更新为 [{status}]"

    return f"错误：未找到任务 #{task_id}"
