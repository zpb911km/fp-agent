"""
Task Create 插件 - 创建新任务

自包含插件，直接读写 tasks.json，不依赖任何全局状态。
"""

import json
from pathlib import Path
from typing import Dict, Any

from ._plugin_config import get_tasks_file


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


def execute(params: dict) -> str:
    subject = params.get("subject")
    description = params.get("description")
    if not subject or not description:
        raise ValueError("task_create 插件需要 subject 和 description 参数")

    tasks_path = Path(get_tasks_file())
    tasks_path.parent.mkdir(parents=True, exist_ok=True)

    # 读取现有任务
    if tasks_path.exists():
        try:
            with open(tasks_path, 'r', encoding='utf-8') as f:
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

    with open(tasks_path, 'w', encoding='utf-8') as f:
        json.dump({"tasks": tasks, "next_id": next_id + 1}, f, ensure_ascii=False, indent=2)

    return f"✅ 已创建任务 #{new_task['id']}: {subject}"
