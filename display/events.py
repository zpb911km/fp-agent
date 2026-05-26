"""事件/消息传递机制 — 逻辑层与显示层之间的单向通知通道。

设计思路:
  逻辑层 emit 事件 → 显示层订阅并渲染，双向不感知对方内部细节。

使用场景:
  1. 流式输出流：逻辑层不断 emit(STREAM_CHUNK, text)
  2. 异步任务进度：逻辑层 emit(TASK_PROGRESS, {id, percent})
  3. 日志/诊断：逻辑层 emit(LOG, msg) 显示层决定是否展示

事件 vs 直接调用:
  - 直接调用 Display 方法：适用于确定性、同步的渲染（如 show_welcome）
  - 事件机制：适用于低频率、解耦更彻底、或未来可能增加订阅者的场景

当前实现: 简单回调式。未来可替换为 asyncio EventBus。
"""

from __future__ import annotations
from enum import Enum, auto
from typing import Any, Callable, Optional


class DisplayEvent(Enum):
    """所有可触发的显示事件。"""

    # 流式输出
    STREAM_BEGIN = auto()
    STREAM_CONTENT = auto()
    STREAM_REASONING = auto()
    STREAM_END = auto()

    # 会话
    SESSION_END = auto()

    # 进度/任务
    TASK_AUTO_START = auto()
    TASK_AUTO_ERROR = auto()
    TASK_AUTO_PAUSED = auto()
    COMPACT_PROGRESS = auto()
    COMPACT_DONE = auto()
    COMPACT_FAILED = auto()

    # 通知
    INFO = auto()
    WARNING = auto()
    ERROR = auto()
    STATUS = auto()

    # 命令反馈
    FORK_CREATED = auto()
    MEMORY_SAVED = auto()


EventHandler = Callable[[DisplayEvent, dict[str, Any]], None]


class EventBus:
    """简单的同步事件总线。

    显示层实现者注册回调，逻辑层通过 emit() 触发事件。
    若不需要事件机制，可直接调用 Display 接口方法——事件是可选通道。
    """

    def __init__(self) -> None:
        self._handlers: dict[DisplayEvent, list[EventHandler]] = {}

    def on(self, event: DisplayEvent, handler: EventHandler) -> None:
        """订阅事件。"""
        self._handlers.setdefault(event, []).append(handler)

    def off(self, event: DisplayEvent, handler: EventHandler) -> None:
        """取消订阅。"""
        handlers = self._handlers.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    def emit(self, event: DisplayEvent, **data: Any) -> None:
        """触发事件，通知所有订阅者。"""
        for handler in self._handlers.get(event, []):
            handler(event, data)

    def clear(self) -> None:
        """清空所有订阅。"""
        self._handlers.clear()


# 全局事件总线（单例）
_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    """获取全局事件总线实例。"""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def reset_event_bus() -> None:
    """重置事件总线（主要用于测试）。"""
    global _bus
    _bus = None
