"""
Task Clear 插件 - 清除已完成的任务

删除所有已完成的 (completed) 任务，并自动维护 next_id。
"""

import json
from pathlib import Path
from typing import Dict, Any

from ._plugin_config import get_tasks_file


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




def execute(params: dict) -> str:
    """
    清除已完成的任务
    
    Args:
        params: 空字典
        
    Returns:
        清除结果
    """
    tasks_path = Path(get_tasks_file())
    
    if not tasks_path.exists():
        return "暂无任务文件"
    
    # 读取现有任务
    try:
        with open(tasks_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            tasks = data.get("tasks", [])
    except Exception as e:
        return f"错误：无法读取任务文件 - {e}"
    
    completed = [t for t in tasks if t.get("status") == "completed"]
    pending = [t for t in tasks if t.get("status") != "completed"]
    removed_count = len(completed)
    
    if removed_count == 0:
        return "没有已完成的任务需要清除"
    
    # 维护 next_id：取剩余任务中最大的 id + 1，如果没任务了就从 1 开始
    if pending:
        next_id = max(t["id"] for t in pending) + 1
    else:
        next_id = 1
    
    # 保存
    with open(tasks_path, 'w', encoding='utf-8') as f:
        json.dump({"tasks": pending, "next_id": next_id}, f, ensure_ascii=False, indent=2)
    
    return f"✅ 已清除 {removed_count} 个已完成任务，剩余 {len(pending)} 个待办任务，下一个任务 ID 为 {next_id}"
