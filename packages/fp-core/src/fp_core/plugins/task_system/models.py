"""任务数据模型"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    """任务状态枚举"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in {s.value for s in cls}


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
