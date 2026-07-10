"""TaskStore — 任务存储层

JSON 文件存储，位于 .fp/tasks.json（项目本地）。
线程安全（所有同步操作均在 executor 中运行）。
"""

import json
import os

from .models import Task, TaskStatus

TASKS_FILE = os.path.join(".fp", "tasks.json")


class TaskStore:
    """任务存储（JSON 文件 CRUD）"""

    def __init__(self, file_path: str | None = None):
        self._file_path = file_path or TASKS_FILE

    @property
    def file_path(self) -> str:
        return self._file_path

    def load(self) -> tuple[list[Task], int]:
        """加载任务列表和下一个 ID"""
        if not os.path.exists(self._file_path):
            return [], 1

        try:
            with open(self._file_path, encoding="utf-8") as f:
                data = json.load(f)
            tasks = [Task.from_dict(t) for t in data.get("tasks", [])]
            next_id = data.get("next_id", 1)
            return tasks, next_id
        except Exception:
            return [], 1

    def save(self, tasks: list[Task], next_id: int):
        """保存任务列表"""
        os.makedirs(os.path.dirname(self._file_path), exist_ok=True)
        with open(self._file_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "tasks": [t.to_dict() for t in tasks],
                    "next_id": next_id,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

    def create(self, subject: str) -> Task:
        """创建新任务"""
        tasks, next_id = self.load()
        task = Task(id=next_id, subject=subject)
        tasks.append(task)
        self.save(tasks, next_id + 1)
        return task

    def update(self, task_id: int, status: str) -> Task | None:
        """更新任务状态"""
        tasks, next_id = self.load()
        for t in tasks:
            if t.id == task_id:
                if not TaskStatus.is_valid(status):
                    raise ValueError(f"无效状态: {status}，必须是 {[s.value for s in TaskStatus]}")
                t.status = TaskStatus(status)
                self.save(tasks, next_id)
                return t
        return None

    def list_all(self) -> list[Task]:
        """列出所有任务"""
        tasks, _ = self.load()
        return tasks

    def clear_completed(self) -> int:
        """清除已完成任务，返回清除数量"""
        tasks, next_id = self.load()
        pending = [t for t in tasks if t.status != TaskStatus.COMPLETED]
        cleared = len(tasks) - len(pending)
        if cleared == 0:
            return 0
        self.save(pending, next_id)
        return cleared

    def summarize(self) -> str | None:
        """生成紧凑状态摘要，供 [task] 提醒使用"""
        tasks, _ = self.load()
        if not tasks:
            return None

        in_progress = [t for t in tasks if t.status == TaskStatus.IN_PROGRESS]
        pending = [t for t in tasks if t.status == TaskStatus.PENDING]

        parts = []
        if in_progress:
            ids = ",".join(str(t.id) for t in in_progress)
            parts.append(f"▶#{ids}")
        if pending:
            parts.append(f"⬜{len(pending)}")

        return " ".join(parts) if parts else None
