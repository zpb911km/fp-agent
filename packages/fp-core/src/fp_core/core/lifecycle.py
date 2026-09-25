"""
生命周期管理系统
基于事件驱动，支持同步/异步钩子

核心设计：
- 所有钩子均为执行钩子（无通知/执行之分）：可修改传入数据、守卫（阻止/取消）流程；
  载荷本身不可变的钩子点（如 ON_SHUTDOWN/ON_CLEANUP）即插件执行注册/清理动作的时机。
- 统一异常处理：任何钩子异常都设置 context.error + 停止传播，
  调用方自主检查 context.error 决定是否处理。
- typed event context：为每个关键事件定义明确的输入/输出字段
  （modified_* = 修改后的值，blocked/cancelled/handled = 守卫位）。
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from functools import wraps
from typing import Any, TypeVar, cast

from fp_core.logger import get_logger

# ═══════════════════════════════════════════════════════════════
# 生命周期钩子枚举
# ═══════════════════════════════════════════════════════════════


class LifecycleHook(Enum):
    """生命周期钩子枚举"""

    # ── 初始化阶段 ──
    ON_INIT = auto()  # Agent 初始化完成 — 插件执行注册（工具/命令等）
    ON_CONFIG_LOADED = auto()  # 配置加载完成 — 插件可改写配置

    # ── 消息处理阶段 ──
    ON_MESSAGE_FILTER = auto()  # 用户消息过滤/修改 — 可修改内容/阻止进入
    ON_MESSAGE_RECEIVED = auto()  # 消息已接收 — 提交前最终裁定：可修改/拦截

    # ── LLM交互阶段 ──
    ON_BEFORE_LLM_CALL = auto()  # LLM调用前 — 可修改 messages/tools，或取消调用
    ON_AFTER_LLM_CALL = auto()  # LLM返回后 — 可修改 response，或阻止工具执行

    # ── 响应阶段 ──
    ON_BEFORE_RESPONSE = auto()  # 返回响应前 — 可修改 content

    # ── 工具执行阶段 ──
    ON_TOOL_SELECT = auto()  # 工具已选择 — 可修改工具列表/阻止执行
    ON_TOOL_CALL = auto()  # 工具即将调用 — 可修改参数/暂停/拒绝
    ON_TOOL_RESULT = auto()  # 工具调用完成 — 可审查/修改/过滤结果
    ON_TOOL_ERROR = auto()  # 工具调用出错 — 可处理/覆盖错误

    # ── 上下文管理 ──
    ON_CONTEXT_UPDATE = auto()  # 上下文已更新 — 可修改本轮对话消息（保存前生效）

    # ── 错误处理 ──
    ON_ERROR = auto()  # 发生错误 — 可覆盖错误信息；handled=True 恢复主循环

    # ── 资源管理 ──
    ON_SHUTDOWN = auto()  # 关闭中 — 插件执行关闭动作
    ON_CLEANUP = auto()  # 清理资源 — 插件执行清理动作


# ═══════════════════════════════════════════════════════════════
# Typed Event Contexts
# ═══════════════════════════════════════════════════════════════


@dataclass
class MessageFilterEvent:
    """ON_MESSAGE_FILTER 的事件上下文"""

    original_content: str  # 原始用户输入
    filtered_content: str = ""  # 修改后的内容（插件可改）
    messages: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])  # 当前对话上下文
    blocked: bool = False  # 守卫：是否阻止消息进入
    block_reason: str = ""  # 阻止原因


@dataclass
class MessageReceivedEvent:
    """ON_MESSAGE_RECEIVED 的事件上下文（提交进上下文前的最终裁定）"""

    content: str  # 已通过过滤的消息
    messages: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])  # 当前对话上下文
    modified_content: str | None = None  # 插件修改后的内容
    blocked: bool = False  # 守卫：阻止该消息提交进上下文
    block_reason: str = ""  # 阻止原因


@dataclass
class BeforeLLMCallEvent:
    """ON_BEFORE_LLM_CALL 的事件上下文"""

    messages: list[dict[str, Any]]  # 传给 LLM 的消息（插件可修改）
    tools: list[dict[str, Any]]  # 工具定义列表
    modified_messages: list[dict[str, Any]] | None = None  # 插件修改后的消息
    cancelled: bool = False  # 守卫：是否取消此次调用
    cancel_reason: str = ""  # 取消原因


@dataclass
class AfterLLMCallEvent:
    """ON_AFTER_LLM_CALL 的事件上下文"""

    response: dict[str, Any]  # LLM 返回的 assistant message
    has_tool_calls: bool = False  # 是否有工具调用
    tool_names: list[str] = field(default_factory=list[str])
    content: str = ""  # 回复文本
    modified_response: dict[str, Any] | None = None  # 插件修改后的 response
    block_tool_execution: bool = False  # 守卫：阻止工具执行


@dataclass
class BeforeResponseEvent:
    """ON_BEFORE_RESPONSE 的事件上下文"""

    content: str  # 最终回复文本
    modified_content: str | None = None  # 插件修改后的回复
    session_id: str = ""  # 当前会话 ID


@dataclass
class ToolSelectEvent:
    """ON_TOOL_SELECT 的事件上下文"""

    tools: list[str]  # 选中的工具名列表（插件可修改）
    modified_tools: list[str] | None = None  # 插件修改后的工具列表
    cancelled: bool = False  # 守卫：阻止所有工具执行
    cancel_reason: str = ""


@dataclass
class ToolCallEvent:
    """ON_TOOL_CALL 的事件上下文"""

    tool_name: str
    tool_args: str  # JSON 字符串
    tool_call_id: str = ""  # 工具调用 ID
    modified_tool_args: str | None = None  # 插件修改后的参数
    cancelled: bool = False  # 守卫：阻止此工具执行
    cancel_reason: str = ""
    pending_review: bool = False  # 等待人工审核


@dataclass
class ToolResultEvent:
    """ON_TOOL_RESULT 的事件上下文"""

    tool_name: str
    result: str  # 原始结果
    tool_call_id: str = ""
    modified_result: str | None = None  # 插件修改后的结果
    blocked: bool = False  # 守卫：阻止结果返回给 LLM
    block_reason: str = ""
    pending_review: bool = False  # 等待人工审核


@dataclass
class ToolErrorEvent:
    """ON_TOOL_ERROR 的事件上下文"""

    tool_name: str
    error: str
    tool_call_id: str = ""
    modified_error: str | None = None  # 插件修改后的错误信息
    suppressed: bool = False  # 守卫：抑制错误，继续流程
    suppress_reason: str = ""


@dataclass
class ContextUpdateEvent:
    """ON_CONTEXT_UPDATE 的事件上下文"""

    msg_count: int
    messages: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])  # 当前对话消息（插件可修改）
    modified_messages: list[dict[str, Any]] | None = None  # 插件修改后的消息列表（整体替换，保存前生效）


@dataclass
class ErrorEvent:
    """ON_ERROR 的事件上下文"""

    error: str  # 原始错误信息
    modified_error: str | None = None  # 插件修改后的错误信息（用于展示/记录）
    handled: bool = False  # 守卫：插件声明已处理该错误，主循环恢复执行（否则 break）
    handled_reason: str = ""  # 处理说明


# ═══════════════════════════════════════════════════════════════


@dataclass
class HookContext:
    """钩子执行上下文（通用）"""

    hook: LifecycleHook
    data: dict[str, Any] = field(default_factory=dict[str, Any])
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])
    stop_propagation: bool = False
    error: Exception | None = None


# 钩子注册条目：(优先级, 名称, 回调函数)
HookEntry = tuple[int, str, Callable[..., Any]]


class LifecycleManager:
    """
    生命周期管理器
    支持同步/异步钩子，按优先级执行
    """

    def __init__(self, enable_log: bool = False):
        self._hooks: dict[str, list[HookEntry]] = {}  # hook_name -> [(priority, name, func)]
        self._enable_log = enable_log
        self._stats: dict[str, int] = {}

    def register(
        self,
        hook: LifecycleHook,
        func: Callable[..., Any],
        priority: int = 100,
        name: str | None = None,
    ):
        """注册钩子函数（所有钩子均为执行钩子，无类型之分）"""
        hook_name = hook.name
        if hook_name not in self._hooks:
            self._hooks[hook_name] = []

        name = cast(str, name or getattr(func, "__name__", str(id(func))))

        # 按优先级插入
        hooks_list = self._hooks[hook_name]
        inserted = False
        for i, (p, _n, _f) in enumerate(hooks_list):
            if priority < p:
                hooks_list.insert(i, (priority, name, func))
                inserted = True
                break
        if not inserted:
            hooks_list.append((priority, name, func))

        if self._enable_log:
            get_logger().info(f"[Lifecycle] Registered '{name}' on {hook.name} (priority={priority})")

    def unregister(self, hook: LifecycleHook, name: str) -> bool:
        """注销钩子"""
        hook_name = hook.name
        if hook_name in self._hooks:
            for i, (_p, n, _f) in enumerate(self._hooks[hook_name]):
                if n == name:
                    self._hooks[hook_name].pop(i)
                    return True
        return False

    async def emit(self, hook: LifecycleHook, context: HookContext | None = None, **kwargs: Any) -> HookContext:
        """
        触发钩子。

        返回最终的上下文（可能被钩子修改）。
        调用方应检查返回的 context.data 来获取插件修改后的值。

        异常处理策略（统一）：
        - 任何钩子抛出异常 → 设置 context.error，停止传播（stop_propagation=True）
        - 不再自动 raise，调用方自主检查 context.error 决定如何处理
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
            get_logger().info(f"[Lifecycle] Emitting {hook.name}...")

        hook_name = hook.name
        if hook_name not in self._hooks or not self._hooks[hook_name]:
            return context

        for _priority, name, func in self._hooks[hook_name]:
            if context.stop_propagation:
                break

            try:
                start = time.time()
                if asyncio.iscoroutinefunction(func):  # pyright: ignore[reportDeprecated]
                    result = await func(context, **context.data)
                else:
                    result = func(context, **context.data)

                elapsed = time.time() - start
                self._stats[f"{hook.name}:{name}"] = self._stats.get(f"{hook.name}:{name}", 0) + 1

                if self._enable_log:
                    get_logger().info(f"[Lifecycle]   -> {name} took {elapsed * 1000:.2f}ms")

                # 如果钩子返回新上下文，合并
                if result is not None and isinstance(result, HookContext):
                    context = result

            except Exception as e:
                context.error = e
                context.stop_propagation = True
                if self._enable_log:
                    get_logger().info(f"[Lifecycle]   -> {name} ERROR: {e}")

        return context

    def clear(self, hook: LifecycleHook | None = None):
        """清除钩子。清除前通过 get_hooks / get_stats 输出诊断信息。"""
        if self._enable_log:
            hooks_info = self.get_hooks()
            stats_info = self.get_stats()
            log = get_logger()
            if hooks_info:
                log.info(f"[Lifecycle] 清除 {len(hooks_info)} 个钩子: {hooks_info}")
            if stats_info:
                log.info(f"[Lifecycle] 统计: {stats_info}")
        if hook:
            self._hooks[hook.name] = []
        else:
            self._hooks.clear()

    def get_hooks(self) -> list[str]:
        """返回所有已注册的钩子名称（扁平列表）"""
        return [name for names in self._hooks.values() for _, name, _ in names]

    def get_stats(self) -> dict[str, int]:
        """获取钩子执行统计"""
        return self._stats.copy()


F = TypeVar("F", bound=Callable[..., Any])


def hook(hook_enum: LifecycleHook, priority: int = 100) -> Callable[[F], F]:
    """装饰器：注册生命周期钩子（保留旧接口）"""

    def decorator(func: F) -> F:
        func._lifecycle_hook = hook_enum  # pyright: ignore[reportFunctionMemberAccess]
        func._lifecycle_priority = priority  # pyright: ignore[reportFunctionMemberAccess]

        @wraps(func)
        async def async_wrapper(context: HookContext, **kwargs: Any) -> Any:
            return await func(context, **kwargs)

        @wraps(func)
        def sync_wrapper(context: HookContext, **kwargs: Any) -> Any:
            return func(context, **kwargs)

        if asyncio.iscoroutinefunction(func):  # pyright: ignore[reportDeprecated]
            return cast(F, async_wrapper)
        return cast(F, sync_wrapper)

    return decorator
