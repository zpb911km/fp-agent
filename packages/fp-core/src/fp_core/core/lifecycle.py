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
    """生命周期钩子枚举

    完整状态机边集定义见 docs/dev/插件系统.md §6.2（与 agent loop 边集清单一一对应）。
    命名约定：pre 边（mutation 前）多带 BEFORE/ENTER/EXEC；post 边（mutation 后）为观察点。
    """

    # ── 初始化阶段 ──
    ON_INIT = auto()  # #1 Agent 初始化完成 — 插件执行注册（工具/命令等）
    ON_INITIALIZED = auto()  # #2 初始化检查通过（首次或幂等跳过）
    ON_CONFIG_LOADED = auto()  # 配置加载完成 — 插件可改写配置（图外：配置装载点）

    # ── 输入/命令阶段 ──
    ON_INPUT = auto()  # #4 输入到达（非命令判定前）— data: content, messages
    ON_EMPTY = auto()  # #3 空输入早退（不落盘）— data: messages
    ON_BEFORE_COMMAND = auto()  # #5 命令开始执行 — data: name, arg；blocked 可阻断
    ON_COMMAND = auto()  # #6 命令输出返回 — data: name, arg, output, handled, latency_ms（journal 必需）
    ON_FALLTHROUGH = auto()  # #7 slash 未处理降级为消息 — data: content
    ON_MSG_ENTER = auto()  # #8 非命令进入消息处理 — data: content, messages
    ON_MESSAGE_BLOCKED = auto()  # #9 消息被插件拦截（早退不落盘）— data: content, block_reason, source
    ON_MESSAGE_FILTER = auto()  # #10 用户消息过滤/修改 — 可修改内容/阻止进入
    ON_MESSAGE_RECEIVED = auto()  # #11 消息已接收 — 提交前最终裁定：可修改/拦截
    ON_RESUME = auto()  # #12 handoff 续接入口（跳过 filter/append）

    # ── LLM循环阶段 ──
    ON_INTERRUPT = auto()  # #13 环顶中断检查抛出前 — data: reason（目前仅 io.info）
    ON_CTX_REPAIR = auto()  # #14 repair_tool_ordering 后 — data: repaired, messages（静默 mutation 观察点）
    ON_EARLY_EXIT = auto()  # #15 插件取消本轮 LLM 调用，早退 — data: reason（不落盘）
    ON_BEFORE_LLM_CALL = auto()  # #16 LLM调用前 — 可修改 messages/tools，或取消调用
    ON_AFTER_LLM_CALL = auto()  # #17 LLM返回后 — 可修改 response，或阻止工具执行；data 含 usage/model/latency_ms
    ON_CANCEL = auto()  # #19 CancelledError/KeyboardInterrupt 再抛出 process — data: reason
    ON_LLM_RETRY = auto()  # #20 tool 顺序错误修复后二次调用（不重过 BEFORE 门）— data: error
    ON_LLM_RETRY_OK = auto()  # #21 重试成功 fall-through 至 AFTER 门
    ON_LLM_FATAL = auto()  # #22/#23 重试失败或非 tool 错误 break — data: error, stage
    ON_STREAM_STRIP = auto()  # #24 interrupted 剥离 tool_calls + 注记后
    ON_TOOL_BLOCKED = auto()  # #25 block_tool_execution 守卫生效 — data: tool_names
    ON_LLM_PASS = auto()  # #26 modified_response 后正常通过守卫 — data: response
    # op ∈ {user, assistant, tool, handoff}
    ON_CTX_APPEND = auto()  # #27/#42/#54 消息入库后 — data: op, message, msg_count, messages
    ON_STREAM_INTERRUPT = auto()  # #28 流式中断 break（本轮已入库 assistant）
    ON_TURN_END = auto()  # #29 无 tool_calls 正常终答 break — data: content
    ON_ITERATION = auto()  # #44 工具轮完成回环顶（continue）

    # ── 响应阶段 ──
    ON_BEFORE_RESPONSE = auto()  # #47 返回响应前 — 可修改 content

    # ── 工具执行阶段 ──
    ON_TOOL_SELECT = auto()  # #31 工具已选择 — 可修改工具列表/阻止执行
    ON_TOOL_CANCELLED = auto()  # #31 SELECT 拦截生效全部跳过 — data: tools, cancel_reason, placeholder_written
    ON_TOOL_CALL = auto()  # #32 工具即将调用 — 可修改参数/暂停/拒绝
    ON_TOOL_REJECTED = auto()  # #33 ON_TOOL_CALL 拒绝生效，仍过 RESULT 门 — data: tool_name, tool_call_id, reason
    ON_TOOL_EXEC = auto()  # #34 参数改写后真正执行 — data: tool_name, tool_args, tool_call_id
    ON_TOOL_RESULT = auto()  # #35 工具调用完成 — 可审查/修改/过滤结果；data 含 latency_ms
    ON_TOOL_ERROR = auto()  # #36 工具调用出错 — 可处理/覆盖错误；data 含 latency_ms
    ON_TOOL_SUPPRESSED = auto()  # #37 错误被抑制直接返回 — data: tool_name, error, suppress_reason
    ON_TOOL_ERROR_PROPAGATED = auto()  # #38 未抑制错误经 gather 传播 — data: tool_name, error
    ON_RESULT_MUTATED = auto()  # #39 RESULT 门 blocked/modified 消费后 — data: tool_name, blocked, has_modified
    ON_TOOL_RESULTS_READY = auto()  # #40 gather 全部返回、按序消费前 — data: results, count
    ON_TOOL_ABORT = auto()  # #41 gather 整体被取消，全量补记 — data: count, reason
    ON_TOOL_PARTIAL = auto()  # #43 遇中断元素：已记 1 + 补记剩余 — data: recorded, remaining

    # ── 上下文管理 ──
    ON_CONTEXT_UPDATE = auto()  # #45 上下文已更新 — 可修改本轮对话消息（保存前生效）
    ON_SESSION_SAVE = auto()  # #46 save_context 覆写磁盘 — data: session_id, path, msg_count, messages

    # ── 错误处理 ──
    ON_ERROR = auto()  # #18 发生错误 — 可覆盖错误信息；handled=True 恢复主循环

    # ── 资源管理 ──
    ON_CONTEXT_RESTORE = auto()  # #49/#50 finally: contextvar 复位（正常返回与异常路径均发）
    ON_SHUTDOWN = auto()  # #51 关闭中 — 插件执行关闭动作
    ON_CLEANUP = auto()  # #52 清理资源 — 插件执行清理动作


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
    usage: dict[str, Any] | None = None  # 本次调用 token 用量（F1：逐调用统计）
    model: str = ""  # 实际模型名
    latency_ms: float = 0.0  # 本次调用耗时
    modified_response: dict[str, Any] | None = None  # 插件修改后的 response
    block_tool_execution: bool = False  # 守卫：阻止工具执行


@dataclass
class CommandEvent:
    """ON_BEFORE_COMMAND / ON_COMMAND 的事件上下文

    ON_COMMAND 是命令的唯一 post 观察点（纯读命令如 /token 只有这里能捕获输出），
    且命令输出直接返回、绕过 ON_BEFORE_RESPONSE。
    """

    name: str  # 命令名（含前导 /）
    arg: str = ""  # 参数
    output: str = ""  # ON_COMMAND：命令输出（post）
    handled: bool = True  # ON_COMMAND：是否被命令系统处理
    latency_ms: float = 0.0  # ON_COMMAND：执行耗时
    blocked: bool = False  # ON_BEFORE_COMMAND 守卫：阻断执行
    block_reason: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])  # 命令执行后的 ctx（journal 行）


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
    latency_ms: float = 0.0  # 该工具执行耗时（F3）
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
    latency_ms: float = 0.0  # 该工具执行耗时（F3）
    modified_error: str | None = None  # 插件修改后的错误信息
    suppressed: bool = False  # 守卫：抑制错误，继续流程
    suppress_reason: str = ""


@dataclass
class CtxAppendEvent:
    """ON_CTX_APPEND 的事件上下文（append 后唯一 post 观察点 — journal 的 ctx-after 行）"""

    op: str  # "user" | "assistant" | "tool" | "handoff"
    message: dict[str, Any] = field(default_factory=dict[str, Any])  # 刚入库的消息
    msg_count: int = 0  # 入库后总消息数
    messages: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])  # 入库后全量 ctx


@dataclass
class SessionSaveEvent:
    """ON_SESSION_SAVE 的事件上下文（save_context 整体覆写磁盘时触发）"""

    session_id: str = ""
    path: str = ""
    msg_count: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])  # 覆写前的最终状态


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
