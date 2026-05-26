"""
Task Update 插件 - 更新任务状态

支持更新任务的执行状态（pending → in_progress → completed）。
"""

import json
from pathlib import Path
from typing import Dict, Any

from ._plugin_config import get_tasks_file


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




def execute(params: dict) -> str:
    """
    更新任务状态
    
    Args:
        params: 包含 'task_id' 和 'status' 键的字典
        
    Returns:
        更新结果
    """
    task_id = params.get("task_id")
    status = params.get("status")
    
    if task_id is None or status is None:
        raise ValueError("task_update 插件需要 task_id 和 status 参数")
    
    valid_statuses = ["pending", "in_progress", "completed"]
    if status not in valid_statuses:
        raise ValueError(f"status 必须是 {valid_statuses} 之一")
    
    tasks_path = Path(get_tasks_file())
    
    if not tasks_path.exists():
        return f"错误：未找到任务文件"
    
    # 读取现有任务
    try:
        with open(tasks_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            tasks = data.get("tasks", [])
    except Exception as e:
        return f"错误：无法读取任务文件 - {e}"
    
    # 查找并更新任务
    for t in tasks:
        if t["id"] == task_id:
            old_status = t["status"]
            t["status"] = status
            save_tasks(tasks)
            return f"✅ 任务 #{task_id} 状态已从 [{old_status}] 更新为 [{status}]"
    
    return f"错误：未找到任务 #{task_id}"


def save_tasks(tasks: list):
    """保存任务列表到文件"""
    tasks_path = Path(get_tasks_file())
    tasks_path.parent.mkdir(parents=True, exist_ok=True)
    
    # 计算 next_id
    next_id = max((t["id"] for t in tasks), default=1) + 1
    
    with open(tasks_path, 'w', encoding='utf-8') as f:
        json.dump({"tasks": tasks, "next_id": next_id}, f, ensure_ascii=False, indent=2)
