"""任务数据模型"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    """任务状态枚举

    生命周期：
      pending → in_progress → delivered → completed   （交付→用户批准）
          ↑                    ↓
          └──── 用户推翻 ──→ superseded（旧交付作废，另立新任务承接）
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in {s.value for s in cls}


#: 终态集合（可被 task_clear 清除）。注意不能放枚举类内：StrEnum 会把 frozenset 误当成员。
TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.SUPERSEDED})


@dataclass
class Task:
    """任务数据模型"""

    id: int
    subject: str
    status: TaskStatus = TaskStatus.PENDING

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject": self.subject,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(
            id=data["id"],
            subject=data.get("subject", ""),
            status=TaskStatus(data.get("status", TaskStatus.PENDING.value)),
        )
