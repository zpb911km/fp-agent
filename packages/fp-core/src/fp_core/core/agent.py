"""
Agent 主干类（全异步版本 — 重构版）

职责收敛为"编排层"：
- 持有注入的服务（ConversationState, LLMService, ToolExecutor, PromptBuilder, SessionManager）
- 主循环 _process_inner 只做流程控制，具体操作委托给服务
- 生命周期 emit() 返回值被实际消费，插件可修改数据、守卫（guard）流程（所有钩子均为执行钩子）
- 不直接持有 _context — ConversationState 是唯一的事实源
"""

import asyncio
import contextlib
import contextvars
import json
import os
import time
import types
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from fp_core import (
    config,  # only for shutdown_panel (CLI-specific)
)
from fp_core.commands import execute as execute_command
from fp_core.commands import get_all_commands
from fp_core.core import session
from fp_core.core.conversation import ConversationState
from fp_core.core.io import IOChannel
from fp_core.core.lifecycle import HookContext, LifecycleHook, LifecycleManager
from fp_core.core.llm_service import LLMConfig, LLMService
from fp_core.core.prompt_builder import PromptBuilder
from fp_core.core.state import State, current_state
from fp_core.core.token_tracker import TokenTracker
from fp_core.core.tool_executor import ToolExecutor
from fp_core.logger import get_logger
from fp_core.plugins.base.plugin import PluginRegistry

# ── 上下文 local IO 通道（防并发竞态） ─────────────────
# ⚠️  contextvars 在 asyncio 中跨 Task 不会自动传播。
#     如果在 _process_inner() 内部通过 asyncio.create_task() 启动了新的子 Task，
#     该子 Task 访问 _current_io.get() 将返回 None（fallback 到 self._default_io）。
#
#     如需在子 Task 中正确传播 IO 通道，使用 copy_context().run() 包装：
#       ctx = contextvars.copy_context()
#       asyncio.create_task(ctx.run(main_coro()))
#
#     或 Python 3.12+：
#       asyncio.create_task(coro, context=contextvars.copy_context())
_current_io: contextvars.ContextVar["IOChannel | None"] = contextvars.ContextVar("_current_io", default=None)


def get_current_io() -> IOChannel | None:
    """获取当前 asyncio Task 的 IO 通道（插件用）

    生命周期钩子函数中调用此方法获取当前环境的 IO 通道：
      - 终端      → CLIIO（使用 input() + display）
      - WebUI      → WebSocketIO（推送到前端）
      - ACP/IDE    → ACPIO（返回 "q"）
      - REST API   → RestIO（返回 ""）

    返回值在 process() 调用期间有效，之后恢复为 None。
    """
    return _current_io.get()


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


class Agent:
    """
    完整 Agent 实现（全异步 — 服务化重构）

    职责：编排主循环 + 触发生命周期
    委托：ConversationState / LLMService / ToolExecutor / PromptBuilder / SessionManager

    特性：
    - 生命周期驱动的插件系统（emit 返回值被消费）
    - 会话管理（持久化通过 SessionStore）
    - 技能系统（已迁移到 memory，通过 memory_read 按需检索）
    - 工具调用（循环执行通过 ToolExecutor）
    - 死循环检测
    - 上下文压缩
    """

    def __init__(
        self,
        enable_log: bool = False,
        resume: str | None = None,
        session_id: str | None = None,
        io: IOChannel | None = None,
        tool_executor: ToolExecutor | None = None,
        prompt_builder: PromptBuilder | None = None,
        role: Any | None = None,  # 🆕 角色定义（AgentRole duck-typed，不引入包依赖）
        on_shutdown: Any | None = None,  # shutdown 回调 fn(**data)，终端用于渲染退出面板
    ):
        self.enable_log = enable_log

        # IO 通道（默认静默）
        self._default_io = io or IOChannel()
        self._shutdown_callback = on_shutdown

        # 检查配置
        if not config.check_llm_config():
            raise ValueError("LLM API 配置不完整")

        # ── 创建 LLM client ──
        from fp_core.core.llm_client import Client

        self.client = Client(api_key=config.LLM_API_KEY, base_url=config.LLM_API_BASE_URL)

        # ── 注入服务 ─────────────────────────────────

        # PromptBuilder：系统提示词构建（技能已迁移到 memory）
        self._prompter = prompt_builder or PromptBuilder()

        # LLMService：纯 LLM 调用
        llm_config = LLMConfig(
            model=config.LLM_MODEL,
            temperature=config.LLM_TEMPERATURE,
            max_tokens=config.LLM_MAX_TOKENS,
            extra_body=config.LLM_EXTRA_BODY,
        )
        self._llm = LLMService(self.client, llm_config, on_usage=self._record_aux_usage)

        # ToolExecutor：工具执行
        # 不传参数 → ToolExecutor 自动创建独立的 ToolRegistry（不再使用全局单例）
        self._tool_exec = tool_executor or ToolExecutor()

        # ── 角色系统：覆盖 system prompt / 工具集 / LLM 配置 ──
        self._role = role

        if role is not None:
            # 角色模式：使用角色的 system prompt
            initial_prompt = getattr(role, "system_prompt", self._prompter.build_system_prompt())

            # 角色覆盖模型配置
            model_override = getattr(role, "llm_model", None)
            if model_override:
                self._llm = LLMService(
                    self.client,
                    LLMConfig(
                        model=model_override,
                        temperature=getattr(role, "temperature", None) or config.LLM_TEMPERATURE,
                        max_tokens=config.LLM_MAX_TOKENS,
                        extra_body=config.LLM_EXTRA_BODY,
                    ),
                    on_usage=self._record_aux_usage,
                )

            # 角色工具白名单过滤
            allowed_tools = getattr(role, "allowed_tools", None)
            if allowed_tools:
                allowed_set = set(allowed_tools)
                _orig_get_defs: Callable[[], list[dict[str, Any]]] = cast(
                    "Callable[[], list[dict[str, Any]]]",
                    self._tool_exec.get_definitions,  # type: ignore[reportUnknownMemberType]
                )
                self._tool_exec.get_definitions = lambda: [  # type: ignore[reportUnknownMemberType]
                    d for d in _orig_get_defs() if d["function"]["name"] in allowed_set
                ]
        else:
            # 超级个体模式：使用标准 system prompt
            initial_prompt = self._prompter.build_system_prompt()

        # ConversationState：上下文状态的唯一所有者
        self._conv = ConversationState(initial_prompt)

        # SessionManager：持久化（不再持有 _context）
        # session_id 非空时使用预置 sid（subagent 场景：父进程预生成，便于兜底补写）
        self.session = session.SessionManager(resume=resume, new_sid=session_id)
        os.makedirs(config.SESSIONS_DIR, exist_ok=True)

        if not os.environ.get("FP_SUBAGENT_QUIET"):
            self.io.info(f"📂 新会话：{self.session.session_id}")

        # 从会话文件恢复历史
        saved = self.session.load_context(initial_prompt)
        if saved:
            self._conv.set_messages(initial_prompt, saved)

        # ── Token 消耗跟踪 ───────────────────────────
        self._token_tracker = TokenTracker()
        # 恢复会话后还原 token_usage（resume 场景）
        meta_token_usage = self.session.meta.get("token_usage")
        if meta_token_usage:
            self._token_tracker = TokenTracker.from_dict(meta_token_usage)

        # 生命周期管理器
        self.lifecycle = LifecycleManager(enable_log=enable_log)

        # 插件注册表
        _builtin_plugin_dir = os.path.join(os.path.dirname(__file__), "..", "plugins")
        self.plugins = PluginRegistry(
            self.lifecycle,
            plugin_dir=os.path.normpath(_builtin_plugin_dir),
        )

        # 三来源用户插件目录（fetched → public → private，后扫描覆盖先扫描 + 警告）
        # 优先级：private > public > fetched（PluginRegistry.scan 内同名覆盖会打警告）
        from fp_core.config import user_dirs

        for _user_plugin_dir in user_dirs("plugins"):
            if os.path.isdir(_user_plugin_dir):
                self.plugins.scan(_user_plugin_dir)

        # ── 核心状态访问接口（命令/插件的「大通道」，公开） ──
        self.state = State(
            conversation=self._conv,
            session=self.session,
            llm=self._llm,
            lifecycle=self.lifecycle,
            plugins=self.plugins,
            tool_exec=self._tool_exec,
            io=self._default_io,
            token_tracker=self._token_tracker,
        )
        self.state.agent = self  # 命令通过此回引访问 Agent 实例

        # 中断标记
        self._interrupted = False
        self._processing = False
        self._cancelled_by_user = False

        # 初始化锁（防竞态）
        self._init_lock = asyncio.Lock()
        self._initialized: bool = False

        # 内置钩子
        self._register_builtin_hooks()

        # 触发初始化生命周期
        # （init 在 ensure_initialized 中触发）

    @property
    def _system_prompt(self) -> str:
        return self._conv.system_prompt

    @property
    def io(self) -> "IOChannel":
        """获取当前异步上下文的 IO 通道（context-local，防并发竞态）

        1. 优先返回 process() 的 context var 覆盖值（如 WebSocketIO）
        2. 无覆盖时退回到 __init__ 注入的默认通道

        ⚠️  跨 asyncio.Task 隔离：
           contextvars 绑定到创建它的 Task，不会自动传播到子 Task。
           如果在子 Task 中访问此属性且父 Task 设置了 context var，
           将返回 None → fallback 到默认通道（而非预期通道）。
           见 _current_io 定义处的传播方案。
        """
        ctx_io: IOChannel | None = _current_io.get()
        return ctx_io if ctx_io is not None else self._default_io

    # ── 公共属性 ─────────────────────────────────────

    @property
    def is_processing(self) -> bool:
        """是否正在处理请求"""
        return self._processing

    @property
    def cancelled_by_user(self) -> bool:
        """是否被用户主动取消"""
        return self._cancelled_by_user

    def reset_cancelled(self):
        """重置用户取消标记"""
        self._cancelled_by_user = False

    @property
    def model(self) -> str:
        """当前模型名称"""
        return self._llm.model

    # ── 内置钩子 ─────────────────────────────────────

    def _register_builtin_hooks(self) -> None:
        self.lifecycle.register(LifecycleHook.ON_INIT, self._on_init, priority=0, name="builtin_init")
        self.lifecycle.register(
            LifecycleHook.ON_SHUTDOWN, self._builtin_shutdown, priority=999, name="builtin_shutdown"
        )

    async def _on_init(self, ctx: HookContext, **kwargs: Any) -> HookContext:
        if self.enable_log:
            get_logger().info("[Agent] Initializing...")
        ctx.data["initialized"] = True
        return ctx

    async def _builtin_shutdown(self, ctx: HookContext, **kwargs: Any) -> HookContext:
        """关闭钩子 — 生成会话摘要 + 保存上下文 + 显示退出面板"""
        # 热重载时不显示关闭面板
        if getattr(self.state, "silent_shutdown", False):
            return ctx

        summary = ""
        if not self.state.nuclear_exit:
            # 统一入口：保存上下文 + 生成摘要
            messages = self._conv.to_serializable()
            summary = self.session.save_and_summarize(messages)

            # 保存 token 消耗到会话 meta
            token_data = self._token_tracker.to_dict()
            if token_data.get("total", {}).get("call_count", 0) > 0:
                self.session.update_meta(token_usage=token_data)

        # 触发 shutdown 回调（终端用于渲染退出面板）
        if self._shutdown_callback and not getattr(self.state, "silent_shutdown", False):
            info = self.session.list_sessions().get(self.session.session_id, {})
            msg_count = info.get("message_count", 0)
            created = info.get("created", "?")
            duration = ""
            try:
                delta = datetime.now() - datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
                h, r = divmod(int(delta.total_seconds()), 3600)
                m, s = divmod(r, 60)
                duration = f"{h}:{m:02d}:{s:02d}"
            except Exception:
                pass

            self._shutdown_callback(
                summary=summary,
                file=f"{self.session.session_id}.jsonl",
                model=self.model,
                msg_count=msg_count,
                created=created,
                duration=duration,
                token_usage=self._token_tracker.total,
            )

        if self.enable_log:
            get_logger().info("[Agent] Shutting down...")
        return ctx

    # ============ 中断机制 ============

    def cancel(self) -> None:
        """请求中断当前处理

        设置中断标记，在下一个循环检查点生效。
        注意：不会立即中断正在进行的 LLM 请求，
        请求完成后会检测标记并停止。
        跨线程安全（GIL 保护原子赋值）。
        """
        self._interrupted = True

    def _check_interrupted(self) -> None:
        """检查中断标记（纯实例方案）

        信号处理器通过 agent.cancel() 设置 self._interrupted，
        也可直接调用 cancel() 中断正在进行的处理。
        """
        if self._interrupted:
            self._interrupted = False
            self._processing = False
            raise asyncio.CancelledError("用户中断")

    # ============ LLM 调用（含 IO 展示） ============

    async def _invoke_llm(
        self, context: list[dict[str, Any]], silent: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """发起流式聊天请求（逐 token 展示）

        实际 LLM 调用委托给 LLMService.chat_stream()。
        可流式（默认）→ 不显示 spinner，token 随到随显；
        chat() 被覆写时（如测试 mock）→ 显示 spinner，降级为非流式。

        Returns:
            (assistant_msg, usage)
            assistant_msg: {"role", "content", "tool_calls"?}
            usage: {"prompt_tokens", "completion_tokens", "total_tokens"} | None
        """
        io = self.io

        # ── 判断是否为真正的流式（而非测试 mock 降级） ──
        _chat = self._llm.chat
        _can_stream = isinstance(_chat, types.MethodType) and _chat.__func__ is LLMService.chat

        if not silent:
            await io.thinking_start(streaming=_can_stream)

        _llm_exc: BaseException | None = None
        usage: dict[str, Any] | None = None
        assistant_msg: dict[str, Any] | None = None
        try:
            async for event in self._llm.chat_stream(
                context,
                tools=cast("list[dict[str, Any]] | None", self._tool_exec.get_definitions()),  # type: ignore[reportUnknownMemberType]
            ):
                if event.type == "content":
                    if not silent:
                        io.stream_write(event.text)
                elif event.type == "reasoning":
                    if not silent:
                        io.stream_think(event.text)
                elif event.type == "usage":
                    usage = event.data
                elif event.type == "done":
                    assistant_msg = event.data
        except asyncio.CancelledError:
            self._cancelled_by_user = True
            msg = {"role": "assistant", "content": "", "_interrupted": True}
            return msg, None
        except Exception as e:
            _llm_exc = e
            raise
        finally:
            if not silent:
                try:
                    await io.thinking_stop()
                except Exception:
                    if _llm_exc is None:
                        raise
                # 无论异常与否都结束流
                with contextlib.suppress(Exception):
                    io.stream_end()

        if assistant_msg is None:
            # 流结束但没拿到 done 事件（异常分支）
            msg = {"role": "assistant", "content": "", "_interrupted": True}
            return msg, usage

        msg = {"role": "assistant", "content": assistant_msg.get("content", "")}
        if assistant_msg.get("reasoning_content"):
            msg["reasoning_content"] = assistant_msg["reasoning_content"]
        if assistant_msg.get("tool_calls"):
            msg["tool_calls"] = assistant_msg["tool_calls"]
        msg["_interrupted"] = False

        return msg, usage

    # ============ 工具展示 ============

    async def _execute_tool(self, tc: dict[str, Any], silent: bool = False) -> str:
        """执行工具（异常逃逸到 _execute_one_tool，由 ON_TOOL_ERROR 生命周期处理）"""
        name = tc["function"]["name"]
        args = json.loads(tc["function"]["arguments"])

        io = self.io
        if not silent:
            io.tool_call(name, args)

        result = await cast("Callable[[dict[str, Any]], Awaitable[str]]", self._tool_exec.execute)(tc)  # type: ignore[reportUnknownMemberType]

        if not silent:
            io.tool_result(result)

        return result

    async def _result_gate(self, tool_name: str, tool_call_id: str, result: str) -> str:
        """RESULT 门（#35 ON_TOOL_RESULT + #39 ON_RESULT_MUTATED）

        三条路径（插件拒绝 / 错误抑制 / 执行成功）统一走本门 — 修复缺陷①（抑制路径原绕门）。
        """
        ctx = await self.lifecycle.emit(
            LifecycleHook.ON_TOOL_RESULT,
            tool_name=tool_name,
            result=result[:5000],
            tool_call_id=tool_call_id,
        )
        blocked = bool(ctx.data.get("blocked"))
        if blocked:
            self.io.warning(f"🔧 工具 {tool_name} 的结果被插件过滤")
            result = ctx.data.get("block_reason", "结果被插件过滤")
        modified_result = ctx.data.get("modified_result")
        has_modified = modified_result is not None
        if has_modified:
            result = modified_result
        # ── #39 blocked/modified_result 消费后（post） ──
        if blocked or has_modified:
            await self.lifecycle.emit(
                LifecycleHook.ON_RESULT_MUTATED,
                tool_name=tool_name,
                blocked=blocked,
                has_modified=has_modified,
                tool_call_id=tool_call_id,
            )
        return result

    async def _append_tool_message(self, tool_call_id: str, content: str) -> None:
        """tool 消息入库 + #42 ON_CTX_APPEND（op=tool — journal 的 ctx-after 行）"""
        msg = self._conv.add_tool_message(tool_call_id, content)
        await self.lifecycle.emit(
            LifecycleHook.ON_CTX_APPEND,
            op="tool",
            message=msg,
            msg_count=len(self._conv.messages),
            messages=self._conv.messages,
        )

    async def _execute_one_tool(self, tc: dict[str, Any], silent: bool = False) -> tuple[str, str]:
        """并行工具执行单元 — 封装单个工具的完整生命周期

        包含：
          1. ON_TOOL_CALL（插件可拒绝/修改参数）→ 拒绝走 #33 ON_TOOL_REJECTED + RESULT 门
          2. 执行工具（#34 ON_TOOL_EXEC — 参数定稿后）
          3. RESULT 门（`_result_gate`）— 拒绝/抑制/成功三路统一
          4. ON_TOOL_ERROR（异常时）→ #37 抑制 / #38 传播

        Returns:
            (tool_call_id, result_string)

        Raises:
            asyncio.CancelledError / KeyboardInterrupt: 用户中断
            Exception: 未被插件抑制的工具执行异常
        """
        tool_name = tc["function"]["name"]

        # ── ON_TOOL_CALL ──
        ctx = await self.lifecycle.emit(
            LifecycleHook.ON_TOOL_CALL,
            tool_name=tool_name,
            tool_args=tc["function"]["arguments"][:5000],
            tool_call_id=tc["id"],
        )

        # 插件拒绝
        if ctx.data.get("cancelled"):
            reason = ctx.data.get("cancel_reason", "工具被插件拒绝")
            self.io.info(f"🔧 插件拒绝工具 {tool_name}: {reason}")
            # ── #33 拒绝生效（post）→ 仍过 RESULT 门（保持行为一致） ──
            await self.lifecycle.emit(
                LifecycleHook.ON_TOOL_REJECTED,
                tool_name=tool_name,
                tool_call_id=tc["id"],
                reason=reason,
            )
            result = await self._result_gate(tool_name, tc["id"], f"被插件拒绝: {reason}")
            return (tc["id"], result)

        # 插件修改参数
        modified_args = ctx.data.get("modified_tool_args")
        if modified_args is not None:
            tc["function"]["arguments"] = modified_args

        # ── #34 参数定稿后、真正执行前（pre） ──
        await self.lifecycle.emit(
            LifecycleHook.ON_TOOL_EXEC,
            tool_name=tool_name,
            tool_args=tc["function"]["arguments"][:5000],
            tool_call_id=tc["id"],
        )

        # 执行工具
        try:
            result = await self._execute_tool(tc, silent=silent)
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise  # 中断→外部 gather 统一处理
        except Exception as e:
            # 工具异常 → 通知插件，看是否可抑制
            self.io.error(f"工具 {tool_name} 执行错误: {e}")
            ctx = await self.lifecycle.emit(
                LifecycleHook.ON_TOOL_ERROR,
                tool_name=tool_name,
                error=str(e),
                tool_call_id=tc["id"],
            )
            if ctx.data.get("suppressed"):
                reason = ctx.data.get("suppress_reason", "插件已抑制错误")
                self.io.warning(f"  ⚠️ 错误已被插件抑制: {reason}")
                # ── #37 抑制生效（post）— 修复缺陷①：抑制结果同样过 RESULT 门 ──
                await self.lifecycle.emit(
                    LifecycleHook.ON_TOOL_SUPPRESSED,
                    tool_name=tool_name,
                    error=str(e),
                    suppress_reason=reason,
                    tool_call_id=tc["id"],
                )
                result = await self._result_gate(tool_name, tc["id"], f"错误已被抑制：{reason}")
                return (tc["id"], result)
            # ── #38 未抑制 → 异常传播（post，由 gather return_exceptions 收集） ──
            await self.lifecycle.emit(
                LifecycleHook.ON_TOOL_ERROR_PROPAGATED,
                tool_name=tool_name,
                error=str(e),
                tool_call_id=tc["id"],
            )
            raise

        # ── #35/#39 RESULT 门（成功路径） ──
        result = await self._result_gate(tool_name, tc["id"], result)
        return (tc["id"], result)

    # ============ 命令处理 ============

    @property
    def commands(self):
        cmds = get_all_commands()
        return {f"/{name}": desc for name, desc in cmds.items()}

    async def handle_command(self, cmd_line: str) -> tuple[bool, str]:
        """处理斜杠命令"""
        if not cmd_line.strip().startswith("/"):
            return (False, "")

        parts = cmd_line.strip().split(maxsplit=1)
        cmd = parts[0].lstrip("/").lower()
        arg = parts[1] if len(parts) > 1 else ""

        return await execute_command(self.state, cmd, arg)

    # ============ 主处理流程（全异步） ============

    async def process(self, user_input: str, io: IOChannel | None = None) -> Response:
        """
        处理用户输入（全异步）

        io: 可选 IO 通道覆盖。WebUI 模式传入 WebSocketIO
           io=None → 使用 self._default_io（默认静默 IO）

        context var 生命周期：
          _current_io.set(io or self._default_io) 在此方法入口调用 → 绑定到当前 asyncio.Task
          _current_io.reset(token) 在 finally 块中恢复 → 保证不泄漏

          注意：_current_io 只对当前 Task 可见。
          如果在 _process_inner 内部创建子 Task，需手动传播 context，
          见 _current_io 定义处的说明。
        """
        await self.ensure_initialized()

        if not user_input.strip():
            await self.lifecycle.emit(
                LifecycleHook.ON_EMPTY,
                content=user_input,
                messages=self._conv.messages,
            )
            return Response(content="")

        # 使用 contextvars 设置 IO 通道（不修改实例变量，防并发竞态）
        # io=None → fallback 到 self._default_io，保证 get_current_io() 始终返回有效值
        token = _current_io.set(io or self._default_io)
        _current_io_ref = _current_io  # 锁住旧引用：防止热重载后 _current_io 指向新 contextvar
        _state_ref = current_state  # 锁旧引用：同上，防热重载后指向新 contextvar
        _state_token = _state_ref.set(self.state)
        try:
            return await self._process_inner(user_input)
        finally:
            _current_io_ref.reset(token)
            _state_ref.reset(_state_token)
            # #49/#50 contextvar 已复位（正常返回与异常路径均走此 finally）
            await self.lifecycle.emit(LifecycleHook.ON_CONTEXT_RESTORE, entry="process")

    async def continue_conversation(self, io: IOChannel | None = None) -> Response:
        """续接模式入口：会话尾部已含未应答的 assistant(tool_calls)/tool 消息时，
        不追加用户消息、不走消息过滤，直接进入 LLM 循环继续对话。

        仅供 reload handoff 使用（契约见 fp_core/core/handoff.py）：
        各入口在启动时消费 handoff 后调用本方法，渲染返回的 Response，
        然后将 state._pending_continue 置回 None。
        """
        await self.ensure_initialized()

        token = _current_io.set(io or self._default_io)
        _current_io_ref = _current_io  # 锁住旧引用：防止热重载后 _current_io 指向新 contextvar
        _state_ref = current_state  # 锁旧引用：同上，防热重载后指向新 contextvar
        _state_token = _state_ref.set(self.state)
        try:
            return await self._process_inner("", continuation=True)
        finally:
            _current_io_ref.reset(token)
            _state_ref.reset(_state_token)
            # #49/#50 同 process：contextvar 复位后观察（异常路径同样触发）
            await self.lifecycle.emit(LifecycleHook.ON_CONTEXT_RESTORE, entry="continue")

    async def _process_inner(self, user_input: str, continuation: bool = False) -> Response:
        """处理用户输入的核心逻辑

        continuation=True（reload 续接专用）：跳过命令检查/消息过滤/用户消息追加，
        直接进入 LLM 调用循环——让模型看到自己的 tool 返回后继续对话。
        """

        self._cancelled_by_user = False

        if not continuation:
            # ── #4 输入到达（journal 的 input 行之一） ──
            await self.lifecycle.emit(
                LifecycleHook.ON_INPUT,
                content=user_input,
                messages=self._conv.messages,
            )

            # ── 检查命令 ──
            if user_input.strip().startswith("/"):
                # ── #5 命令开始执行（pre，可阻断/改写） ──
                parts = user_input.strip().split(maxsplit=1)
                cmd_name = parts[0]
                cmd_arg = parts[1] if len(parts) > 1 else ""
                bc_ctx = await self.lifecycle.emit(
                    LifecycleHook.ON_BEFORE_COMMAND,
                    name=cmd_name,
                    arg=cmd_arg,
                    messages=self._conv.messages,
                )
                if bc_ctx.data.get("blocked"):
                    block_reason = bc_ctx.data.get("block_reason", "命令被插件阻断")
                    # ── #6b 命令被阻断（post — 守卫分支的出口，与 ON_MESSAGE_BLOCKED 对称） ──
                    await self.lifecycle.emit(
                        LifecycleHook.ON_COMMAND_BLOCKED,
                        name=cmd_name,
                        arg=cmd_arg,
                        block_reason=block_reason,
                        messages=self._conv.messages,
                    )
                    return Response(
                        content=block_reason,
                        metadata={"from_command": True},
                    )

                t0 = time.monotonic()
                handled, output = await self.handle_command(user_input)
                latency_ms = (time.monotonic() - t0) * 1000.0

                # ── #6 命令输出返回（post，journal 必需：纯读命令唯一观察点） ──
                await self.lifecycle.emit(
                    LifecycleHook.ON_COMMAND,
                    name=cmd_name,
                    arg=cmd_arg,
                    output=output,
                    handled=handled,
                    latency_ms=latency_ms,
                    messages=self._conv.messages,
                )

                if handled:
                    # 命令输出走单一通路：Response.content
                    # 不再额外 emit ON_BEFORE_RESPONSE（前端从 done.final_content 消费）
                    return Response(content=output, metadata={"from_command": True})

                # ── #7 slash 未处理降级为消息 ──
                await self.lifecycle.emit(
                    LifecycleHook.ON_FALLTHROUGH,
                    content=user_input,
                    messages=self._conv.messages,
                )
            else:
                # ── #8 非命令进入消息处理 ──
                await self.lifecycle.emit(
                    LifecycleHook.ON_MSG_ENTER,
                    content=user_input,
                    messages=self._conv.messages,
                )

            # ── 生命周期：MESSAGE_FILTER（插件可修改/拒绝） ──
            ctx = await self.lifecycle.emit(
                LifecycleHook.ON_MESSAGE_FILTER,
                content=user_input,
                messages=self._conv.messages,
            )
            if ctx.data.get("blocked"):
                # ── #9 消息被插件拦截（source=filter） ──
                await self.lifecycle.emit(
                    LifecycleHook.ON_MESSAGE_BLOCKED,
                    content=user_input,
                    block_reason=ctx.data.get("block_reason", "消息被插件过滤"),
                    source="filter",
                    messages=self._conv.messages,
                )
                return Response(content=ctx.data.get("block_reason", "消息被插件过滤"))
            filtered_input = ctx.data.get("filtered_content", user_input)

            # ── 生命周期：消息已接收（提交前最终裁定，插件可修改/拦截） ──
            ctx = await self.lifecycle.emit(
                LifecycleHook.ON_MESSAGE_RECEIVED,
                content=filtered_input,
                messages=self._conv.messages,
            )
            if ctx.data.get("blocked"):
                # ── #9 消息被插件拦截（source=received） ──
                await self.lifecycle.emit(
                    LifecycleHook.ON_MESSAGE_BLOCKED,
                    content=filtered_input,
                    block_reason=ctx.data.get("block_reason", "消息被插件拦截"),
                    source="received",
                    messages=self._conv.messages,
                )
                return Response(content=ctx.data.get("block_reason", "消息被插件拦截"))
            filtered_input = ctx.data.get("modified_content", filtered_input)

            # ── 添加用户消息 + post 观察（journal 的 ctx-after 行） ──
            user_msg = self._conv.add_user_message(filtered_input)
            await self.lifecycle.emit(
                LifecycleHook.ON_CTX_APPEND,
                op="user",
                message=user_msg,
                msg_count=len(self._conv.messages),
                messages=self._conv.messages,
            )
        else:
            # ── #12 handoff 续接入口（跳过 filter/append） ──
            await self.lifecycle.emit(
                LifecycleHook.ON_RESUME,
                messages=self._conv.messages,
            )

        # ── 子 agent 静默模式：抑制 spinner / LLM 流等 UI 输出 ──
        _silent = os.environ.get("FP_SUBAGENT_SILENT") == "1"

        while True:
            # ── 中断检查（支持 signal handler 和 cancel() 两种途径） ──
            # ── #13 环顶中断即将抛出（pre — raise 前给插件观察机会） ──
            if self._interrupted:
                await self.lifecycle.emit(
                    LifecycleHook.ON_INTERRUPT,
                    reason="用户中断",
                    location="loop_top",
                )
            try:
                self._check_interrupted()
            except asyncio.CancelledError:
                # ── #13b 环顶中断实际抛出（post — 与 LLM 侧 ON_CANCEL 对称的取消事实记录） ──
                await self.lifecycle.emit(
                    LifecycleHook.ON_CANCEL,
                    reason="用户中断",
                    stage="loop_top",
                )
                raise

            # ── #14 修复 tool ordering（post — 静默 mutation 观察点，仅修复发生时触发） ──
            repaired = self._conv.repair_tool_ordering()
            if repaired:
                await self.lifecycle.emit(
                    LifecycleHook.ON_CTX_REPAIR,
                    repaired=repaired,
                    messages=self._conv.messages,
                )

            # ── 生命周期：BEFORE_LLM_CALL（插件可修改 messages / 取消） ──
            ctx = await self.lifecycle.emit(
                LifecycleHook.ON_BEFORE_LLM_CALL,
                messages=self._conv.messages,
                tools=cast("list[dict[str, Any]]", self._tool_exec.get_definitions()),  # type: ignore[reportUnknownMemberType]
            )
            if ctx.data.get("cancelled"):
                # ── #15 插件取消本轮 LLM 调用，早退（post — 注意：不落盘，已知弱点保持原状） ──
                await self.lifecycle.emit(
                    LifecycleHook.ON_EARLY_EXIT,
                    reason=ctx.data.get("cancel_reason", "插件取消"),
                    stage="before_llm_call",
                )
                return Response(content=ctx.data.get("cancel_reason", "已取消"))

            messages_for_llm = ctx.data.get(
                "modified_messages",
                self._conv.get_messages_for_llm(
                    # 本轮请求是否携带 tools —— 决定 reasoning_content 回传策略：
                    # DeepSeek v4 官方要求带 tools 时回传历史思维链；不带 tools 时
                    # 回传会被忽略，直接剥离省 token
                    with_tools=bool(self._tool_exec.get_definitions()),  # type: ignore[reportUnknownMemberType]
                ),
            )

            self._processing = True
            _llm_t0 = time.perf_counter()
            _usage: dict[str, Any] | None = None
            try:
                assistant_msg, _usage = await self._invoke_llm(messages_for_llm, silent=_silent)
                if _usage:
                    self._token_tracker.accumulate(_usage, model=self.model)
            except (asyncio.CancelledError, KeyboardInterrupt):
                self._processing = False
                # ── #19 调用中断直抛 process（post — 无钩子的异常传播路径） ──
                await self.lifecycle.emit(
                    LifecycleHook.ON_CANCEL,
                    reason="LLM 调用被中断/取消",
                    stage="llm_call",
                    latency_ms=(time.perf_counter() - _llm_t0) * 1000.0,
                )
                raise
            except Exception as e:
                self._processing = False
                err_ctx = await self.lifecycle.emit(LifecycleHook.ON_ERROR, error=str(e))
                if err_ctx.data.get("handled"):
                    # 插件声明已处理该错误 → 恢复主循环（环顶仍有 interrupt 检查兜底）
                    self.io.warning(f"♻️ 错误已被插件处理，恢复主循环: {err_ctx.data.get('handled_reason') or e}")
                    continue
                self.io.error(f"API/LLM 错误: {err_ctx.data.get('modified_error') or e}")
                err_str = str(e)
                if "'tool'" in err_str and "preceding" in err_str:
                    self.io.warning("  🔧 检测到 tool 顺序错误，二次修复...")
                    self._conv.repair_tool_ordering()
                    # ── #20 修复后二次调用（pre — 不重过 ON_BEFORE_LLM_CALL 门） ──
                    await self.lifecycle.emit(
                        LifecycleHook.ON_LLM_RETRY,
                        error=err_str,
                        stage="repair_then_retry",
                    )
                    _retry_t0 = time.perf_counter()
                    try:
                        self._processing = True
                        assistant_msg, _usage2 = await self._invoke_llm(self._conv.messages, silent=_silent)
                        if _usage2:
                            self._token_tracker.accumulate(_usage2, model=self.model)
                        _usage = _usage2  # 重试成功后与主路径对齐，供 AFTER 门 usage 字段消费
                    except (asyncio.CancelledError, KeyboardInterrupt):
                        self._processing = False
                        await self.lifecycle.emit(
                            LifecycleHook.ON_CANCEL,
                            reason="LLM 重试被中断/取消",
                            stage="llm_retry",
                            latency_ms=(time.perf_counter() - _retry_t0) * 1000.0,
                        )
                        raise
                    except Exception as e2:
                        self._processing = False
                        self.io.error(f"  ❌ 修复后仍失败: {e2}")
                        # ── #22 重试仍失败 → break（post） ──
                        await self.lifecycle.emit(
                            LifecycleHook.ON_LLM_FATAL,
                            error=str(e2),
                            stage="retry_failed",
                        )
                        break
                    # ── #21 重试成功 fall-through 至 AFTER 门（post） ──
                    await self.lifecycle.emit(
                        LifecycleHook.ON_LLM_RETRY_OK,
                        error=err_str,
                        latency_ms=(time.perf_counter() - _retry_t0) * 1000.0,
                    )
                else:
                    # ── #23 非 tool 错误 break（post — 本轮无新响应） ──
                    await self.lifecycle.emit(
                        LifecycleHook.ON_LLM_FATAL,
                        error=err_str,
                        stage="unrecoverable",
                    )
                    break
            _llm_latency_ms = (time.perf_counter() - _llm_t0) * 1000.0
            self._processing = False

            # ── 生命周期：AFTER_LLM_CALL（插件可修改回复 / 拦截工具执行） ──
            # ── #17 post(LLM)/pre(append)：usage/model/latency 字段补强（F1） ──
            tc_names = [tc["function"]["name"] for tc in assistant_msg.get("tool_calls", [])]
            ctx = await self.lifecycle.emit(
                LifecycleHook.ON_AFTER_LLM_CALL,
                response=assistant_msg,
                has_tool_calls=bool(tc_names),
                tool_names=tc_names,
                content=assistant_msg.get("content", ""),
                usage=_usage,
                model=self.model,
                latency_ms=_llm_latency_ms,
            )
            if ctx.data.get("modified_response"):
                assistant_msg = ctx.data["modified_response"]
            _tool_blocked = bool(ctx.data.get("block_tool_execution"))
            if _tool_blocked:
                # ── #25 block_tool_execution 守卫生效（post） ──
                _blocked_names = [tc["function"]["name"] for tc in assistant_msg.get("tool_calls", [])]
                assistant_msg.pop("tool_calls", None)
                await self.lifecycle.emit(
                    LifecycleHook.ON_TOOL_BLOCKED,
                    tool_names=_blocked_names,
                )

            # 流式中断处理
            _stripped = False
            interrupted = assistant_msg.pop("_interrupted", False)
            if interrupted and "tool_calls" in assistant_msg:
                tc_names = [tc["function"]["name"] for tc in assistant_msg["tool_calls"]]
                content = assistant_msg.get("content", "")
                note = f"\n\n[用户中断 — 计划调用的工具: {', '.join(tc_names)}，请求已被用户打断]"
                assistant_msg["content"] = (content + note) if content else note.strip()
                del assistant_msg["tool_calls"]
                _stripped = True
            if _stripped:
                # ── #24 interrupted 剥离 tool_calls + 注记后（post） ──
                await self.lifecycle.emit(
                    LifecycleHook.ON_STREAM_STRIP,
                    tool_names=tc_names,
                )
            if not _tool_blocked and not _stripped:
                # ── #26 守卫通过（含 modified_response 后） → 正常入库（post） ──
                # 与 #24/#25 互斥：三者是"响应后处理 → assistant入库"同一转移的替代结果
                await self.lifecycle.emit(
                    LifecycleHook.ON_LLM_PASS,
                    response=assistant_msg,
                )

            assistant_msg = self._conv.add_assistant_message(assistant_msg)

            # ── #27 assistant 消息入库后（post — journal 关键缺口，原为零钩子） ──
            await self.lifecycle.emit(
                LifecycleHook.ON_CTX_APPEND,
                op="assistant",
                message=assistant_msg,
                msg_count=len(self._conv.messages),
                messages=self._conv.messages,
            )

            if interrupted:
                self.io.info("⏹️ 已中断（保留了已生成的内容）")
                # ── #28 流式中断 break（post — 本轮已入库 assistant） ──
                await self.lifecycle.emit(
                    LifecycleHook.ON_STREAM_INTERRUPT,
                    content=assistant_msg.get("content", ""),
                )
                break

            # ── 处理工具调用 ──
            tool_calls = assistant_msg.get("tool_calls", [])
            tool_interrupted = False
            if tool_calls:
                sel_tool_names = [tc["function"]["name"] for tc in tool_calls]
                ctx = await self.lifecycle.emit(LifecycleHook.ON_TOOL_SELECT, tools=sel_tool_names)
                if ctx.data.get("cancelled"):
                    select_reason = ctx.data.get("cancel_reason", "无原因")
                    self.io.warning(f"⏹️ 工具执行被插件拦截: {select_reason}")
                    # 修复缺陷②：全部跳过时为每个 tool_call 补写占位结果，避免悬挂
                    for tc in tool_calls:
                        await self._append_tool_message(tc["id"], f"被插件拦截（未执行）: {select_reason}")
                    # ── #31 SELECT 拦截生效（post） ──
                    await self.lifecycle.emit(
                        LifecycleHook.ON_TOOL_CANCELLED,
                        tools=sel_tool_names,
                        cancel_reason=select_reason,
                        placeholder_written=True,
                        msg_count=len(self._conv.messages),
                    )
                    break
                # 如果插件修改了工具列表，按新列表过滤
                modified_tools = ctx.data.get("modified_tools")
                if modified_tools is not None:
                    skipped = [
                        tc["function"]["name"] for tc in tool_calls if tc["function"]["name"] not in modified_tools
                    ]
                    if skipped:
                        self.io.info(f"🔧 插件过滤了工具: {', '.join(skipped)}")
                    tool_calls = [tc for tc in tool_calls if tc["function"]["name"] in modified_tools]

                # ═══════════════════════════════════════════════════════════
                # 并行执行所有工具（asyncio.gather + return_exceptions）
                #   - 每个工具独立触发 ON_TOOL_CALL → 执行 → ON_TOOL_RESULT
                #   - 结果列表保持与 tool_calls 相同的顺序
                # ═══════════════════════════════════════════════════════════
                try:
                    raw_results: list[tuple[str, str] | BaseException] = await asyncio.gather(
                        *[self._execute_one_tool(tc, silent=_silent) for tc in tool_calls],
                        return_exceptions=True,
                    )
                except (asyncio.CancelledError, KeyboardInterrupt):
                    # gather 自身被取消（如 signal handler 中断）→ 标记全部中断
                    self._interrupted = False
                    self._cancelled_by_user = True
                    for tc in tool_calls:
                        await self._append_tool_message(tc["id"], "工具调用失败：用户中断")
                    # ── #41 gather 整体被取消，全量补记完成（post） ──
                    await self.lifecycle.emit(
                        LifecycleHook.ON_TOOL_ABORT,
                        count=len(tool_calls),
                        reason="gather cancelled",
                    )
                    self.io.info("⏹️ 已中断（上下文已保留工具调用信息）")
                    break

                # ── #40 gather 全部返回、按序消费前（pre） ──
                await self.lifecycle.emit(
                    LifecycleHook.ON_TOOL_RESULTS_READY,
                    results=raw_results,
                    count=len(raw_results),
                )

                # ── 按序处理结果（保持与 LLM 返回顺序一致） ──
                for tc, result_or_exc in zip(tool_calls, raw_results, strict=True):
                    if isinstance(result_or_exc, (asyncio.CancelledError, KeyboardInterrupt)):
                        # 用户中断 — 此工具及其后的工具均未完成
                        self._interrupted = False
                        self._cancelled_by_user = True
                        await self._append_tool_message(tc["id"], "工具调用失败：用户中断")
                        tool_interrupted = True
                        break
                    elif isinstance(result_or_exc, Exception):
                        # 工具异常（未被插件抑制）→ 记录错误，不中断其他工具
                        err_msg = f"❌ 工具执行失败 ({tc['function']['name']}): {result_or_exc}"
                        self.io.error(err_msg)
                        await self._append_tool_message(tc["id"], err_msg)
                    else:
                        assert isinstance(result_or_exc, tuple), f"预期 tuple，实际 {type(result_or_exc)}"
                        tid, result = result_or_exc
                        await self._append_tool_message(tid, result)

                if tool_interrupted:
                    # 补全未处理工具的中断记录（for break 后剩余的 tool_calls）
                    break_out_idx = next(
                        (
                            i
                            for i, r in enumerate(raw_results)
                            if isinstance(r, (asyncio.CancelledError, KeyboardInterrupt))
                        ),
                        len(tool_calls),
                    )
                    for remaining in tool_calls[break_out_idx + 1 :]:
                        await self._append_tool_message(remaining["id"], "未执行（工具调用失败：用户中断）")
                    # ── #43 已记 1 + 补记剩余完成（post） ──
                    await self.lifecycle.emit(
                        LifecycleHook.ON_TOOL_PARTIAL,
                        recorded=break_out_idx + 1,
                        remaining=len(tool_calls) - break_out_idx - 1,
                    )
                    self.io.info("⏹️ 已中断（上下文已保留工具调用信息）")
                    break
                # ── #44 工具轮完成、回环顶再次调 LLM（post） ──
                await self.lifecycle.emit(
                    LifecycleHook.ON_ITERATION,
                    tools_executed=len(tool_calls),
                    msg_count=len(self._conv.messages),
                )
                continue  # 工具全部完成 → 回到 while 循环（再次调用 LLM）
            else:
                # ── #29 无 tool_calls 正常终答 break（post） ──
                await self.lifecycle.emit(
                    LifecycleHook.ON_TURN_END,
                    content=assistant_msg.get("content", ""),
                    msg_count=len(self._conv.messages),
                )
                break  # 无工具调用 → 跳出 while 循环

        # ── 生命周期：上下文已更新（插件可修改本轮消息，保存前生效） ──
        ctx = await self.lifecycle.emit(
            LifecycleHook.ON_CONTEXT_UPDATE,
            msg_count=len(self._conv),
            messages=self._conv.messages,
        )
        modified_messages = ctx.data.get("modified_messages")
        if modified_messages is not None:
            self._conv.replace_all(modified_messages)

        # 保存上下文 — #46 覆写磁盘前发出 SESSION_SAVE（journal 状态存档点：
        # 本行 messages=覆写后的最终状态，journal 上一行即被剪枝前状态）
        _to_save = self._conv.to_serializable()
        await self.lifecycle.emit(
            LifecycleHook.ON_SESSION_SAVE,
            session_id=self.session.session_id,
            path=self.session.get_session_path(),
            msg_count=len(_to_save),
            messages=_to_save,
        )
        self.session.save_context(_to_save)

        # 提取最终回复
        final_content = self._conv.get_last_content()

        # 清理中断标志
        self._interrupted = False
        self._processing = False

        # ── 生命周期：返回响应前（插件可修改最终回复） ──
        ctx = await self.lifecycle.emit(
            LifecycleHook.ON_BEFORE_RESPONSE,
            content=final_content,
            session_id=self.session.session_id,
        )
        final_content = ctx.data.get("modified_content", final_content)

        return Response(content=final_content)

    # ── #53 面外隐性调用的 usage 回流（summarize 等不经主循环，此前 usage 被丢弃）──
    def _record_aux_usage(self, usage: dict[str, Any] | None, model: str) -> None:
        """LLMService.on_usage 回调：把隐性 LLM 调用的 usage 并入 TokenTracker 聚合统计"""
        self._token_tracker.accumulate(usage, model=model)

    # ============ 生命周期管理 ============

    async def ensure_initialized(self) -> None:
        """确保已触发初始化钩子（线程安全，asyncio.Lock 保护）

        向 ON_INIT 传递 tool_registry，供插件注册工具。
        插件可通过 context.data.system_prompt_append 返回要附加到 system prompt 的文本
        （单个 str = 向后兼容覆盖式；list[str] = 多插件收集，见 apply_system_prompt_append）。
        """
        async with self._init_lock:
            if hasattr(self, "_initialized") and self._initialized:
                await self.lifecycle.emit(LifecycleHook.ON_INITIALIZED, first_time=False)
                return
            self._initialized = True

            ctx = await self.lifecycle.emit(
                LifecycleHook.ON_INIT,
                tool_registry=self._tool_exec.registry,
                state=self.state,
            )

            # 插件可通过 system_prompt_append 追加内容到 system prompt
            # （统一交给 helper：归一化 str/list、过滤空白段、空行拼接）
            from fp_core.prompts import apply_system_prompt_append

            apply_system_prompt_append(self._conv, ctx.data.get("system_prompt_append"))
            await self.lifecycle.emit(LifecycleHook.ON_INITIALIZED, first_time=True)

    async def shutdown(self) -> None:
        """关闭 Agent（清理生命周期钩子 + 释放连接池）"""
        await self.lifecycle.emit(LifecycleHook.ON_SHUTDOWN)
        await self.lifecycle.emit(LifecycleHook.ON_CLEANUP)

        # 清理所有生命周期钩子（防止热重载后旧钩子残留）
        self.lifecycle.clear()

        # 关闭 LLM client 连接池（httpx.AsyncClient）
        with contextlib.suppress(Exception):
            await self.client.close()

        # _on_shutdown 钩子已保存上下文，此处只需处理核弹模式
        if self.state.nuclear_exit:
            self.session.delete_session(self.session.session_id, force=True)
            self.io.info("💥 核弹模式：当前会话已删除，不留痕迹")

        if not getattr(self.state, "silent_shutdown", False):
            self.io.info("👋 Agent 已关闭")
