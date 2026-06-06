"""
Task Create 插件 — 创建新任务

自包含插件，直接读写 tasks.json，不依赖任何全局状态。
"""

import json
import os
from typing import Any, Dict

import config


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


def execute(params: Dict[str, Any]) -> str:
    """
    创建新任务
    
    Args:
        params: 包含 subject, description 的字典
        
    Returns:
        创建结果
    """
    subject = params.get("subject", "")
    description = params.get("description", "")
    
    if not subject or not description:
        raise ValueError("task_create 需要 subject 和 description 参数")
    
    tasks_path = config.TASKS_FILE
    os.makedirs(os.path.dirname(tasks_path), exist_ok=True)
    
    # 读取现有任务
    if os.path.exists(tasks_path):
        try:
            with open(tasks_path, "r", encoding="utf-8") as f:
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
