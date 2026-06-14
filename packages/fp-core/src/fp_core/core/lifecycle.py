"""
生命周期管理系统
基于事件驱动，支持同步/异步钩子

核心设计：
- observe 型钩子：只通知，不改流程。异常被隔离。
- transform 型钩子：可修改传入数据、守卫（阻止/取消）流程。异常会传播。
- typed event context：为每个关键事件定义明确的输入/输出字段。
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from functools import wraps
from typing import Any

# ═══════════════════════════════════════════════════════════════
# 生命周期钩子枚举
# ═══════════════════════════════════════════════════════════════


class LifecycleHook(Enum):
    """生命周期钩子枚举"""

    # ── 初始化阶段 ──
    ON_INIT = auto()  # Agent 初始化完成
    ON_CONFIG_LOADED = auto()  # 配置加载完成

    # ── 消息处理阶段（transform: 可修改/过滤消息） ──
    ON_MESSAGE_FILTER = auto()  # 【transform】用户消息过滤/修改
    ON_MESSAGE_RECEIVED = auto()  # 【observe】消息已接收（仅通知）

    # ── LLM交互阶段（transform: 可修改入参/出参） ──
    ON_BEFORE_LLM_CALL = auto()  # 【transform】LLM调用前 — 可修改 messages/tools，或取消调用
    ON_AFTER_LLM_CALL = auto()  # 【transform】LLM返回后 — 可修改 response，或阻止工具执行

    # ── 响应阶段（transform: 可修改最终回复） ──
    ON_BEFORE_RESPONSE = auto()  # 【transform】返回响应前 — 可修改 content

    # ── 工具执行阶段（observe: 仅通知） ──
    ON_TOOL_SELECT = auto()  # 【observe】工具已选择
    ON_TOOL_CALL = auto()  # 【observe】工具即将调用
    ON_TOOL_RESULT = auto()  # 【observe】工具调用完成
    ON_TOOL_ERROR = auto()  # 【observe】工具错误

    # ── 上下文管理（observe） ──
    ON_CONTEXT_UPDATE = auto()  # 【observe】上下文已更新

    # ── 错误处理（observe） ──
    ON_ERROR = auto()  # 【observe】发生错误

    # ── 资源管理（observe） ──
    ON_SHUTDOWN = auto()  # 【observe】关闭中
    ON_CLEANUP = auto()  # 【observe】清理资源


# ═══════════════════════════════════════════════════════════════
# Typed Event Contexts
# ═══════════════════════════════════════════════════════════════


@dataclass
class MessageFilterEvent:
    """ON_MESSAGE_FILTER 的事件上下文（transform）"""

    original_content: str  # 原始用户输入
    filtered_content: str = ""  # 修改后的内容（插件可改）
    messages: list[dict] = field(default_factory=list)  # 当前对话上下文
    blocked: bool = False  # 守卫：是否阻止消息进入
    block_reason: str = ""  # 阻止原因


@dataclass
class BeforeLLMCallEvent:
    """ON_BEFORE_LLM_CALL 的事件上下文（transform）"""

    messages: list[dict]  # 传给 LLM 的消息（插件可修改）
    tools: list[dict]  # 工具定义列表
    modified_messages: list[dict] | None = None  # 插件修改后的消息
    cancelled: bool = False  # 守卫：是否取消此次调用
    cancel_reason: str = ""  # 取消原因


@dataclass
class AfterLLMCallEvent:
    """ON_AFTER_LLM_CALL 的事件上下文（transform）"""

    response: dict  # LLM 返回的 assistant message
    has_tool_calls: bool = False  # 是否有工具调用
    tool_names: list[str] = field(default_factory=list)
    content: str = ""  # 回复文本
    modified_response: dict | None = None  # 插件修改后的 response
    block_tool_execution: bool = False  # 守卫：阻止工具执行


@dataclass
class BeforeResponseEvent:
    """ON_BEFORE_RESPONSE 的事件上下文（transform）"""

    content: str  # 最终回复文本
    modified_content: str | None = None  # 插件修改后的回复
    session_id: str = ""  # 当前会话 ID


@dataclass
class ToolCallEvent:
    """ON_TOOL_CALL 的事件上下文（observe）"""

    tool_name: str
    tool_args: str


@dataclass
class ToolResultEvent:
    """ON_TOOL_RESULT 的事件上下文（observe）"""

    tool_name: str
    result: str


@dataclass
class ContextUpdateEvent:
    """ON_CONTEXT_UPDATE 的事件上下文（observe）"""

    msg_count: int


@dataclass
class ErrorEvent:
    """ON_ERROR 的事件上下文（observe）"""

    error: str


# ═══════════════════════════════════════════════════════════════


@dataclass
class HookContext:
    """钩子执行上下文（通用）"""

    hook: LifecycleHook
    data: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    stop_propagation: bool = False
    error: Exception | None = None
    # 标注此钩子点是 observe 还是 transform
    hook_type: str = "observe"  # "observe" | "transform"


class LifecycleManager:
    """
    生命周期管理器
    支持同步/异步钩子，按优先级执行
    """

    def __init__(self, enable_log: bool = False):
        self._hooks: dict[str, list[tuple]] = {}  # hook_name -> [(priority, name, func, hook_type)]
        self._enable_log = enable_log
        self._stats: dict[str, int] = {}

    def register(
        self,
        hook: LifecycleHook,
        func: Callable,
        priority: int = 100,
        name: str | None = None,
        hook_type: str | None = None,  # "observe" | "transform"，None=自动推断
    ):
        """注册钩子函数"""
        hook_name = hook.name
        if hook_name not in self._hooks:
            self._hooks[hook_name] = []

        name = name or getattr(func, "__name__", str(id(func)))

        # 自动推断钩子类型
        if hook_type is None:
            # transform 型钩子列表
            _transform_hooks = {
                "ON_MESSAGE_FILTER",
                "ON_BEFORE_LLM_CALL",
                "ON_AFTER_LLM_CALL",
                "ON_BEFORE_RESPONSE",
            }
            hook_type = "transform" if hook_name in _transform_hooks else "observe"

        # 按优先级插入
        hooks_list = self._hooks[hook_name]
        inserted = False
        for i, (p, _n, _f, _ht) in enumerate(hooks_list):
            if priority < p:
                hooks_list.insert(i, (priority, name, func, hook_type))
                inserted = True
                break
        if not inserted:
            hooks_list.append((priority, name, func, hook_type))

        if self._enable_log:
            print(f"[Lifecycle] Registered '{name}' ({hook_type}) on {hook.name} (priority={priority})")

    def unregister(self, hook: LifecycleHook, name: str) -> bool:
        """注销钩子"""
        hook_name = hook.name
        if hook_name in self._hooks:
            for i, (_p, n, _f, _ht) in enumerate(self._hooks[hook_name]):
                if n == name:
                    self._hooks[hook_name].pop(i)
                    return True
        return False

    async def emit(self, hook: LifecycleHook, context: HookContext | None = None, **kwargs) -> HookContext:
        """
        触发钩子。

        返回最终的上下文（可能被 transform 型钩子修改）。
        调用方应检查返回的 context.data 来获取插件修改后的值。
        """
        if context is None:
            context = HookContext(hook=hook)

        # 合并 kwargs 到 context.data
        for key, value in kwargs.items():
            if key == "data":
                context.data.update(value)
            else:
                context.data[key] = value

        if self._enable_log:
            print(f"[Lifecycle] Emitting {hook.name}...")

        hook_name = hook.name
        if hook_name not in self._hooks or not self._hooks[hook_name]:
            return context

        for _priority, name, func, ht in self._hooks[hook_name]:
            if context.stop_propagation:
                break

            try:
                start = time.time()
                if asyncio.iscoroutinefunction(func):
                    result = await func(context, **context.data)
                else:
                    result = func(context, **context.data)

                elapsed = time.time() - start
                self._stats[f"{hook.name}:{name}"] = self._stats.get(f"{hook.name}:{name}", 0) + 1

                if self._enable_log:
                    print(f"[Lifecycle]   -> {name} took {elapsed * 1000:.2f}ms")

                # 如果钩子返回新上下文，合并
                if result is not None and isinstance(result, HookContext):
                    context = result

            except Exception as e:
                context.error = e
                if ht == "transform":
                    # transform 型钩子异常 → 停止传播，向上报告
                    context.stop_propagation = True
                    if self._enable_log:
                        print(f"[Lifecycle]   -> {name} (transform) ERROR: {e}")
                else:
                    # observe 型钩子异常 → 只记录错误，不中断流程
                    if self._enable_log:
                        print(f"[Lifecycle]   -> {name} (observe) ERROR: {e} (isolated)")
                # 对于 transform 钩子，立即抛出以让调用方知晓
                if ht == "transform" and not isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)):
                    raise

        return context

    def emit_sync(self, hook: LifecycleHook, context: HookContext | None = None, **kwargs) -> HookContext:
        """同步触发钩子（用于非异步环境）"""
        if context is None:
            context = HookContext(hook=hook)

        for key, value in kwargs.items():
            if key == "data":
                context.data.update(value)
            else:
                context.data[key] = value

        hook_name = hook.name
        if hook_name not in self._hooks or not self._hooks[hook_name]:
            return context

        for _priority, name, func, ht in self._hooks[hook_name]:
            if context.stop_propagation:
                break
            try:
                result = func(context, **context.data)
                if result is not None and isinstance(result, HookContext):
                    context = result
            except Exception as e:
                context.error = e
                if ht == "transform":
                    context.stop_propagation = True
                if self._enable_log:
                    print(f"[Lifecycle]   -> {name} ERROR: {e}")

        return context

    def get_hooks(self, hook: LifecycleHook) -> list[str]:
        """获取已注册的所有钩子名称"""
        return [name for _, name, _, _ in self._hooks.get(hook.name, [])]

    def clear(self, hook: LifecycleHook | None = None):
        """清除钩子"""
        if hook:
            self._hooks[hook.name] = []
        else:
            self._hooks.clear()

    def get_stats(self) -> dict[str, int]:
        """获取钩子执行统计"""
        return self._stats.copy()


def hook(hook_type: LifecycleHook, priority: int = 100):
    """装饰器：注册生命周期钩子（保留旧接口）"""

    def decorator(func):
        func._lifecycle_hook = hook_type
        func._lifecycle_priority = priority

        @wraps(func)
        async def async_wrapper(context, **kwargs):
            return await func(context, **kwargs)

        @wraps(func)
        def sync_wrapper(context, **kwargs):
            return func(context, **kwargs)

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator
