"""消息与响应数据类型（轻量模块 — 无重依赖，供 api 层稳定导出）

历史：Message/Response 原定义于 core/agent.py；agent 仍 re-export 保持向后兼容。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Message:
    """消息对象"""

    role: str = "user"
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass
class Response:
    """响应对象"""

    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])
    error: str | None = None


__all__ = ["Message", "Response"]
