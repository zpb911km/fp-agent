"""事件层 — EventBus（出向事件总线）+ EventBridge（lifecycle → 事件转发）

分层说明：
  EventBus 历史上定义在 fp-webui（fp_webui/main.py），导致 core 的 WebSocketIO
  只能用 Any 兜底（分层方向错误）。现下沉至 core：EventBus 与 WebSocketIO 同层，
  反向依赖消除；前端（fp-webui 等）经 ``fp_core.api`` 导入同一总线实例。

  EventBridge 是 core 内置的生命周期转发插件：把 Agent 关键生命周期钩子
  转为事件总线上的事件（原 webui 侧 WebUIPlugin 的桥接职责上移归核），
  任何前端经 ``portal.subscribe()`` / ``portal.events`` 即可消费，
  前端不再各自注册桥接插件。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from fp_core.core.lifecycle import HookContext, LifecycleHook
from fp_core.logger import get_logger
from fp_core.plugins.base.plugin import Plugin

if TYPE_CHECKING:
    from fp_core.core.lifecycle import LifecycleManager

__all__ = ["EVENT_TYPES", "Event", "EventBus", "EventBridge"]

# ── 事件类型目录（协议冻结，新增只做加法） ─────────────────────
# EventBridge 发布（lifecycle 转发） + IOChannel/WebSocketIO 发布（轮内输出）。
# 前端传输层事件（done/cancelled/connected/reload 等）由前端自行发布到同一总线，
# 不属于 core 目录，故不在本集合内。
EVENT_TYPES: frozenset[str] = frozenset({
    # EventBridge（lifecycle → 总线）
    "llm_start",
    "llm_end",
    "tool_select",
    "tool_call",
    "tool_result",
    "error",
    "shutdown",
    # WebSocketIO（轮内输出）
    "info",
    "warning",
    "thinking",
    "chunk",
    "thinking_content",
    "stream_end",
    "ask",
})

# 事件载荷形状：{"type": str, "seq": int(总线补), "ts": float(桥补), **payload}
Event = dict[str, Any]


class EventBus:
    """
    异步事件总线，用于 Agent 生命周期事件 → 前端的桥梁。

    支持多个订阅者（多个 WebSocket 连接），自动清理断开连接。

    背压保护：
      队列（maxsize=1024）满时不丢弃订阅者，而是丢弃最旧事件，
      保证订阅者始终能拿到最新事件，且不会失去连接。

    断连续传（L2）：
      所有事件带全局递增 seq，并写入环形缓冲（deque maxlen）。
      重连的连接用 (run_id, last_seq) 协商重放；缓冲溢出（gap）则降级
      由前端 REST 全量拉取会话历史。
    """

    def __init__(self, buffer_size: int = 2000):
        self._subscribers: dict[str, asyncio.Queue[Event]] = {}
        self._next_id = 0
        # 本次进程的运行标识：服务端重启后 seq 空间重置，
        # 前端凭 run_id 变化丢弃旧 last_seq，避免序号错位导致事件被误过滤。
        self.run_id = secrets.token_hex(4)
        self._seq = 0
        self._buffer: deque[Event] = deque(maxlen=buffer_size)

    @property
    def current_seq(self) -> int:
        """最新事件序号（0 = 尚无事件）"""
        return self._seq

    def events_since(self, last_seq: int) -> list[Event] | None:
        """取回序号 > last_seq 的缓冲事件；缓冲溢出（gap）返回 None。

        前端应据此降级为 REST 全量重拉（resync）。
        """
        if last_seq >= self._seq:
            return []
        if not self._buffer:
            # 有事件但缓冲为空 → 必然是被清空/溢出，按 gap 处理
            return None
        oldest = self._buffer[0]["seq"]
        if last_seq + 1 < oldest:
            return None  # gap：last_seq 之后的部分事件已被挤出缓冲
        return [e for e in self._buffer if e["seq"] > last_seq]

    def subscribe(self) -> tuple[str, asyncio.Queue[Event]]:
        """订阅事件流，返回 (subscriber_id, queue)"""
        sub_id = f"sub_{self._next_id}"
        self._next_id += 1
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=1024)
        self._subscribers[sub_id] = q
        return sub_id, q

    def unsubscribe(self, sub_id: str) -> None:
        """取消订阅"""
        self._subscribers.pop(sub_id, None)

    async def publish(self, event: Event) -> None:
        """向所有订阅者推送事件

        背压策略：队列满时丢弃最旧事件（get_nowait），而非丢弃订阅者。
        确保订阅者不会因消费慢而被静默移除。
        """
        # 先编号入缓冲，再分发（同一同步块内完成，保证 seq 与入队顺序一致）
        self._seq += 1
        event = {**event, "seq": self._seq}
        self._buffer.append(event)

        dead_subs: list[str] = []
        for sub_id, q in self._subscribers.items():
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()  # 丢弃最旧事件
                    q.put_nowait(event)  # 重试放入最新事件
                    get_logger().warning(f"[EventBus] ⚠️ 订阅者 {sub_id} 队列满，已丢弃最旧事件")
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    dead_subs.append(sub_id)  # 保护性断开
        for sub_id in dead_subs:
            self._subscribers.pop(sub_id, None)
            get_logger().warning(f"[EventBus] ⚠️ 订阅者 {sub_id} 因队列异常已被断开")

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def shutdown(self) -> None:
        """关闭所有订阅者"""
        dead_subs = list(self._subscribers.keys())
        for sub_id in dead_subs:
            self._subscribers.pop(sub_id, None)


class EventBridge(Plugin):
    """lifecycle → EventBus 转发桥（core 内置，portal.open 时注册）

    事件形状与原 webui WebUIPlugin 完全一致（前端 JS 协议冻结兼容）：
      llm_start / llm_end(content, has_tool_calls, tool_names) /
      tool_select(tools) / tool_call(name, args, tool_call_id) /
      tool_result(name, result≤200, tool_call_id) / error(error) / shutdown
    """

    name = "event_bridge"
    version = "1.0.0"

    def __init__(self, bus: EventBus):
        super().__init__()
        self._bus = bus

    async def _emit(self, event_type: str, **data: Any) -> None:
        await self._bus.publish({"type": event_type, "ts": time.time(), **data})

    def on_register(self, lifecycle: LifecycleManager) -> None:
        lifecycle.register(
            LifecycleHook.ON_BEFORE_LLM_CALL,
            self._on_before_llm,
            priority=5,
            name="bridge_before_llm",
        )
        lifecycle.register(
            LifecycleHook.ON_AFTER_LLM_CALL,
            self._on_after_llm,
            priority=5,
            name="bridge_after_llm",
        )
        lifecycle.register(
            LifecycleHook.ON_TOOL_SELECT,
            self._on_tool_select,
            priority=5,
            name="bridge_tool_select",
        )
        lifecycle.register(
            LifecycleHook.ON_TOOL_CALL,
            self._on_tool_call,
            priority=5,
            name="bridge_tool_call",
        )
        lifecycle.register(
            LifecycleHook.ON_TOOL_RESULT,
            self._on_tool_result,
            priority=5,
            name="bridge_tool_result",
        )
        lifecycle.register(
            LifecycleHook.ON_ERROR,
            self._on_error,
            priority=5,
            name="bridge_error",
        )
        lifecycle.register(
            LifecycleHook.ON_SHUTDOWN,
            self._on_shutdown,
            priority=5,
            name="bridge_shutdown",
        )

    async def _on_before_llm(self, ctx: HookContext, **kwargs: Any) -> None:
        await self._emit("llm_start")

    async def _on_after_llm(self, ctx: HookContext, **kwargs: Any) -> None:
        await self._emit(
            "llm_end",
            content=kwargs.get("content", ""),
            has_tool_calls=kwargs.get("has_tool_calls", False),
            tool_names=kwargs.get("tool_names", []),
        )

    async def _on_tool_select(self, ctx: HookContext, **kwargs: Any) -> None:
        await self._emit("tool_select", tools=kwargs.get("tools", []))

    async def _on_tool_call(self, ctx: HookContext, **kwargs: Any) -> None:
        await self._emit(
            "tool_call",
            name=kwargs.get("tool_name", ""),
            args=kwargs.get("tool_args", ""),
            tool_call_id=kwargs.get("tool_call_id", ""),
        )

    async def _on_tool_result(self, ctx: HookContext, **kwargs: Any) -> None:
        result = kwargs.get("result", "")
        await self._emit(
            "tool_result",
            name=kwargs.get("tool_name", ""),
            result=(result[:200] + "...") if len(result) > 200 else result,
            tool_call_id=kwargs.get("tool_call_id", ""),
        )

    async def _on_error(self, ctx: HookContext, **kwargs: Any) -> None:
        await self._emit("error", error=str(kwargs.get("error", "")))

    async def _on_shutdown(self, ctx: HookContext, **kwargs: Any) -> None:
        await self._emit("shutdown")

    def on_unregister(self) -> None:
        """卸载钩子由 PluginRegistry 统一清理（见 unregister）"""
