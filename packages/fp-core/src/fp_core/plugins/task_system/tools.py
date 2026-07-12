"""Task 工具处理函数

4 个工具：task_create, task_update, task_list, task_clear
"""

from typing import Any

from .store import TaskStore

# ── OpenAI Function Calling Schema ─────────────────────────────

DEF_CREATE = {
    "type": "function",
    "function": {
        "name": "task_create",
        "description": "创建一个任务用于跟踪多步骤工作进度",
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "任务标题（简短描述，如'实现登录模块'）",
                },
            },
            "required": ["subject"],
        },
    },
}

DEF_UPDATE = {
    "type": "function",
    "function": {
        "name": "task_update",
        "description": "更新已有任务的状态",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "任务 ID",
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed"],
                    "description": "新状态：pending=待办, in_progress=进行中, completed=已完成",
                },
            },
            "required": ["task_id", "status"],
        },
    },
}

DEF_LIST = {
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

DEF_CLEAR = {
    "type": "function",
    "function": {
        "name": "task_clear",
        "description": "清除所有已完成的任务。会删除已完成（completed）状态的记录，并更新任务 ID 序列。",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}

# ── 工具定义列表（方便批量注册） ─────────────────────

ALL_DEFINITIONS: list[dict[str, Any]] = [DEF_CREATE, DEF_UPDATE, DEF_LIST, DEF_CLEAR]

# ── 状态标签（用于显示） ──────────────────────────────

STATUS_LABELS = {
    "pending": "待办",
    "in_progress": "进行中",
    "completed": "已完成",
}


# ── 处理函数 ─────────────────────────────────────────


async def handle_create(params: dict[str, Any]) -> str:
    """创建新任务"""
    subject = params.get("subject", "").strip()
    if not subject:
        raise ValueError("task_create 需要 subject 参数")

    store = TaskStore()
    task = store.create(subject)
    return f"✅ 已创建任务 #{task.id}: {task.subject}"


async def handle_update(params: dict[str, Any]) -> str:
    """更新任务状态"""
    task_id = params.get("task_id")
    status = params.get("status")

    if task_id is None or status is None:
        raise ValueError("task_update 需要 task_id 和 status 参数")

    store = TaskStore()
    task = store.update(task_id, status)
    if task is None:
        return f"错误：未找到任务 #{task_id}"
    return f"✅ 任务 #{task.id} 状态已更新为 [{status}]"


async def handle_list(params: dict[str, Any]) -> str:
    """列出所有任务"""
    store = TaskStore()
    tasks = store.list_all()

    if not tasks:
        return "暂无任务"

    lines = ["📋 任务列表:", ""]
    for t in sorted(tasks, key=lambda x: x.id):
        label = STATUS_LABELS.get(t.status.value, t.status.value)
        lines.append(f"  #{t.id} [{label:>9}] {t.subject}")

    return "\n".join(lines)


async def handle_clear(params: dict[str, Any]) -> str:
    """清除已完成任务"""
    store = TaskStore()
    cleared = store.clear_completed()
    if cleared == 0:
        return "没有已完成的任务需要清除"
    remaining = len(store.list_all())
    return f"✅ 已清除 {cleared} 个已完成任务，剩余 {remaining} 个待办任务"
