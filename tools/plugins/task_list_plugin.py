"""
Task List 插件 — 列出所有任务

自包含插件，直接读取 tasks.json，不依赖任何全局状态。
"""

import json
import os
from typing import Any, Dict

import config


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


def execute(params: Dict[str, Any]) -> str:
    """
    列出所有任务
    
    Args:
        params: 空字典（无参数）
        
    Returns:
        格式化任务列表
    """
    tasks_path = config.TASKS_FILE
    if not os.path.exists(tasks_path):
        return "暂无任务"
    
    try:
        with open(tasks_path, "r", encoding="utf-8") as f:
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
