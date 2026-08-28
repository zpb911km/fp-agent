"""TaskStore — 任务存储层

JSON 文件存储，位于 .fp/tasks.json（项目本地）。
线程安全（所有同步操作均在 executor 中运行）。
"""

import contextlib
import json
import os
from typing import Any, cast

from fp_core.logger import get_logger

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
        """加载任务列表和下一个 ID。

        容错策略：文件不存在 → 正常空；JSON 损坏 → 告警 + 备份原文件后返回空
        （不静默清空，避免任务"凭空消失"）；单条坏数据 → 跳过并告警，保留其余好条目。
        """
        if not os.path.exists(self._file_path):
            return [], 1

        try:
            with open(self._file_path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            get_logger().error(f"[task] ⚠️ tasks.json 解析失败（{e}），已备份为 {self._file_path}.bak")
            with contextlib.suppress(OSError):
                os.replace(self._file_path, self._file_path + ".bak")
            return [], 1

        if not isinstance(data, dict):
            get_logger().warning("[task] ⚠️ tasks.json 顶层结构异常（非对象），按空任务处理")
            return [], 1

        data = cast(dict[str, Any], data)

        tasks: list[Task] = []
        for idx, item in enumerate(data.get("tasks", [])):
            try:
                tasks.append(Task.from_dict(item))
            except Exception as e:
                get_logger().warning(f"[task] ⚠️ 跳过坏任务条目 #{idx}: {e}")

        next_id = data.get("next_id", 1)
        return tasks, next_id

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

        parts: list[str] = []
        if in_progress:
            ids = ",".join(str(t.id) for t in in_progress)
            parts.append(f"▶#{ids}")
        if pending:
            parts.append(f"⬜{len(pending)}")

        return " ".join(parts) if parts else None
