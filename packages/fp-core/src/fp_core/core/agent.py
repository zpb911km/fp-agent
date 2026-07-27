"""
Agent 主干类（全异步版本 — 重构版）

职责收敛为"编排层"：
- 持有注入的服务（ConversationState, LLMService, ToolExecutor, PromptBuilder, SessionManager）
- 主循环 _process_inner 只做流程控制，具体操作委托给服务
- 生命周期 emit() 返回值被实际消费，插件能 transform/guard 流程
- 不直接持有 _context — ConversationState 是唯一的事实源
"""

import asyncio
import contextlib
import contextvars
import json
import os
import types
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

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
from fp_core.core.state import State
from fp_core.core.token_tracker import TokenTracker
from fp_core.core.tool_executor import ToolExecutor
from fp_core.logger import get_logger
from fp_core.platform_utils import get_data_dir
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
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Response:
    """响应对象"""

    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
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
        )
        self._llm = LLMService(self.client, llm_config)

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
                    ),
                )

            # 角色工具白名单过滤
            allowed_tools = getattr(role, "allowed_tools", None)
            if allowed_tools:
                allowed_set = set(allowed_tools)
                _orig_get_defs = self._tool_exec.get_definitions
                self._tool_exec.get_definitions = lambda: [
                    d for d in _orig_get_defs() if d["function"]["name"] in allowed_set
                ]
        else:
            # 超级个体模式：使用标准 system prompt
            initial_prompt = self._prompter.build_system_prompt()

        # ConversationState：上下文状态的唯一所有者
        self._conv = ConversationState(initial_prompt)

        # SessionManager：持久化（不再持有 _context）
        self.session = session.SessionManager(resume=resume)
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

        # 用户插件目录（跨平台，同名覆盖）
        _user_plugin_dir = os.path.join(get_data_dir(), "plugins")
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

    async def _on_init(self, ctx: HookContext, **kwargs) -> HookContext:
        if self.enable_log:
            get_logger().info("[Agent] Initializing...")
        ctx.data["initialized"] = True
        return ctx

    async def _builtin_shutdown(self, ctx: HookContext, **kwargs) -> HookContext:
        """关闭钩子 — 生成会话摘要 + 保存上下文 + 显示退出面板"""
        # 热重载时不显示关闭面板
        if getattr(self.state, "silent_shutdown", False):
            return ctx

        summary = ""
        if not self.state.nuclear_exit:
            last_msgs = self._conv.get_non_system_messages()
            if last_msgs:
                last_user = next((m for m in reversed(last_msgs) if m["role"] == "user"), None)
                if last_user:
                    summary = last_user.get("content", "").strip().replace("\n", " ")[:20]
                    self.session.update_meta(summary=summary)

            # 保存 token 消耗到会话 meta
            token_data = self._token_tracker.to_dict()
            if token_data.get("total", {}).get("call_count", 0) > 0:
                self.session.update_meta(token_usage=token_data)

        if not self.state.nuclear_exit:
            self.session.save_context(self._conv.to_serializable())

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

    async def _invoke_llm(self, context: list[dict], silent: bool = False) -> tuple[dict[str, Any], dict | None]:
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
        usage: dict | None = None
        assistant_msg: dict | None = None
        try:
            async for event in self._llm.chat_stream(context, tools=self._tool_exec.get_definitions()):
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

        result = await self._tool_exec.execute(tc)

        if not silent:
            io.tool_result(result)

        return result

    async def _execute_one_tool(self, tc: dict, silent: bool = False) -> tuple[str, str]:
        """并行工具执行单元 — 封装单个工具的完整生命周期

        包含：
          1. ON_TOOL_CALL（插件可拒绝/修改参数）
          2. 执行工具
          3. ON_TOOL_RESULT（插件可审查/修改/过滤结果）
          4. ON_TOOL_ERROR（异常时）

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
            result = f"被插件拒绝: {reason}"
            # 仍然触发 ON_TOOL_RESULT（保持与原串行行为一致）
            ctx = await self.lifecycle.emit(
                LifecycleHook.ON_TOOL_RESULT,
                tool_name=tool_name,
                result=result,
                tool_call_id=tc["id"],
            )
            if ctx.data.get("blocked"):
                result = ctx.data.get("block_reason", "结果被插件过滤")
            modified_result = ctx.data.get("modified_result")
            if modified_result is not None:
                result = modified_result
            return (tc["id"], result)

        # 插件修改参数
        modified_args = ctx.data.get("modified_tool_args")
        if modified_args is not None:
            tc["function"]["arguments"] = modified_args

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
                return (tc["id"], f"错误已被抑制：{reason}")
            # 未被抑制 → 让异常传播到外部处理
            raise

        # ── ON_TOOL_RESULT ──
        ctx = await self.lifecycle.emit(
            LifecycleHook.ON_TOOL_RESULT,
            tool_name=tool_name,
            result=result[:5000],
            tool_call_id=tc["id"],
        )
        if ctx.data.get("blocked"):
            self.io.warning(f"🔧 工具 {tool_name} 的结果被插件过滤")
            result = ctx.data.get("block_reason", "结果被插件过滤")
        modified_result = ctx.data.get("modified_result")
        if modified_result is not None:
            result = modified_result

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
            return Response(content="")

        # 使用 contextvars 设置 IO 通道（不修改实例变量，防并发竞态）
        # io=None → fallback 到 self._default_io，保证 get_current_io() 始终返回有效值
        token = _current_io.set(io or self._default_io)
        _current_io_ref = _current_io  # 锁住旧引用：防止热重载后 _current_io 指向新 contextvar
        try:
            return await self._process_inner(user_input)
        finally:
            _current_io_ref.reset(token)

    async def _process_inner(self, user_input: str) -> Response:
        """处理用户输入的核心逻辑"""

        self._cancelled_by_user = False

        # ── 检查命令 ──
        if user_input.strip().startswith("/"):
            handled, output = await self.handle_command(user_input)
            if handled:
                # 命令输出走单一通路：Response.content
                # 不再额外 emit ON_BEFORE_RESPONSE（前端从 done.final_content 消费）
                return Response(content=output, metadata={"from_command": True})

        # ── 生命周期：MESSAGE_FILTER（插件可 transform/拒绝） ──
        ctx = await self.lifecycle.emit(
            LifecycleHook.ON_MESSAGE_FILTER,
            content=user_input,
            messages=self._conv.messages,
        )
        if ctx.data.get("blocked"):
            return Response(content=ctx.data.get("block_reason", "消息被插件过滤"))
        filtered_input = ctx.data.get("filtered_content", user_input)

        # ── 添加用户消息 ──
        self._conv.add_user_message(filtered_input)

        # ── 生命周期：消息已接收 ──
        await self.lifecycle.emit(LifecycleHook.ON_MESSAGE_RECEIVED, content=filtered_input)

        # ── 子 agent 静默模式：抑制 spinner / LLM 流等 UI 输出 ──
        _silent = os.environ.get("FP_SUBAGENT_SILENT") == "1"

        while True:
            # ── 中断检查（支持 signal handler 和 cancel() 两种途径） ──
            self._check_interrupted()

            # 修复 tool ordering
            self._conv.repair_tool_ordering()

            # ── 生命周期：BEFORE_LLM_CALL（插件可修改 messages / 取消） ──
            ctx = await self.lifecycle.emit(
                LifecycleHook.ON_BEFORE_LLM_CALL,
                messages=self._conv.messages,
                tools=self._tool_exec.get_definitions(),
            )
            if ctx.data.get("cancelled"):
                return Response(content=ctx.data.get("cancel_reason", "已取消"))

            messages_for_llm = ctx.data.get("modified_messages", self._conv.get_messages_for_llm())

            self._processing = True
            try:
                assistant_msg, _usage = await self._invoke_llm(messages_for_llm, silent=_silent)
                if _usage:
                    self._token_tracker.accumulate(_usage, model=self.model)
            except (asyncio.CancelledError, KeyboardInterrupt):
                self._processing = False
                raise
            except Exception as e:
                self._processing = False
                self.io.error(f"API/LLM 错误: {e}")
                await self.lifecycle.emit(LifecycleHook.ON_ERROR, error=str(e))
                err_str = str(e)
                if "'tool'" in err_str and "preceding" in err_str:
                    self.io.warning("  🔧 检测到 tool 顺序错误，二次修复...")
                    self._conv.repair_tool_ordering()
                    try:
                        self._processing = True
                        assistant_msg, _usage2 = await self._invoke_llm(self._conv.messages, silent=_silent)
                        if _usage2:
                            self._token_tracker.accumulate(_usage2, model=self.model)
                    except (asyncio.CancelledError, KeyboardInterrupt):
                        self._processing = False
                        raise
                    except Exception as e2:
                        self._processing = False
                        self.io.error(f"  ❌ 修复后仍失败: {e2}")
                        break
                else:
                    break
            self._processing = False

            # ── 生命周期：AFTER_LLM_CALL（插件可修改回复 / 拦截工具执行） ──
            tc_names = [tc["function"]["name"] for tc in assistant_msg.get("tool_calls", [])]
            ctx = await self.lifecycle.emit(
                LifecycleHook.ON_AFTER_LLM_CALL,
                response=assistant_msg,
                has_tool_calls=bool(tc_names),
                tool_names=tc_names,
                content=assistant_msg.get("content", ""),
            )
            if ctx.data.get("modified_response"):
                assistant_msg = ctx.data["modified_response"]
            if ctx.data.get("block_tool_execution"):
                # 插件阻止了工具执行
                assistant_msg.pop("tool_calls", None)

            # 流式中断处理
            interrupted = assistant_msg.pop("_interrupted", False)
            if interrupted and "tool_calls" in assistant_msg:
                tc_names = [tc["function"]["name"] for tc in assistant_msg["tool_calls"]]
                content = assistant_msg.get("content", "")
                note = f"\n\n[用户中断 — 计划调用的工具: {', '.join(tc_names)}，请求已被用户打断]"
                assistant_msg["content"] = (content + note) if content else note.strip()
                del assistant_msg["tool_calls"]

            self._conv.add_assistant_message(assistant_msg)

            if interrupted:
                self.io.info("⏹️ 已中断（保留了已生成的内容）")
                break

            # ── 处理工具调用 ──
            tool_calls = assistant_msg.get("tool_calls", [])
            tool_interrupted = False
            if tool_calls:
                sel_tool_names = [tc["function"]["name"] for tc in tool_calls]
                ctx = await self.lifecycle.emit(LifecycleHook.ON_TOOL_SELECT, tools=sel_tool_names)
                if ctx.data.get("cancelled"):
                    self.io.warning(f"⏹️ 工具执行被插件拦截: {ctx.data.get('cancel_reason', '无原因')}")
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
                        self._conv.add_tool_message(tc["id"], "工具调用失败：用户中断")
                    self.io.info("⏹️ 已中断（上下文已保留工具调用信息）")
                    break

                # ── 按序处理结果（保持与 LLM 返回顺序一致） ──
                for tc, result_or_exc in zip(tool_calls, raw_results, strict=True):
                    if isinstance(result_or_exc, (asyncio.CancelledError, KeyboardInterrupt)):
                        # 用户中断 — 此工具及其后的工具均未完成
                        self._interrupted = False
                        self._cancelled_by_user = True
                        self._conv.add_tool_message(tc["id"], "工具调用失败：用户中断")
                        tool_interrupted = True
                        break
                    elif isinstance(result_or_exc, Exception):
                        # 工具异常（未被插件抑制）→ 记录错误，不中断其他工具
                        err_msg = f"❌ 工具执行失败 ({tc['function']['name']}): {result_or_exc}"
                        self.io.error(err_msg)
                        self._conv.add_tool_message(tc["id"], err_msg)
                    else:
                        assert isinstance(result_or_exc, tuple), f"预期 tuple，实际 {type(result_or_exc)}"
                        tid, result = result_or_exc
                        self._conv.add_tool_message(tid, result)

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
                        self._conv.add_tool_message(remaining["id"], "未执行（工具调用失败：用户中断）")
                    self.io.info("⏹️ 已中断（上下文已保留工具调用信息）")
                    break
                continue  # 工具全部完成 → 回到 while 循环（再次调用 LLM）
            else:
                break  # 无工具调用 → 跳出 while 循环

        # ── 生命周期：上下文已更新 ──
        await self.lifecycle.emit(
            LifecycleHook.ON_CONTEXT_UPDATE,
            msg_count=len(self._conv),
        )

        # 保存上下文
        self.session.save_context(self._conv.to_serializable())

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

    # ============ 生命周期管理 ============

    async def ensure_initialized(self) -> None:
        """确保已触发初始化钩子（线程安全，asyncio.Lock 保护）

        向 ON_INIT 传递 tool_registry，供插件注册工具。
        插件可通过 context.data.system_prompt_append 返回要附加到 system prompt 的文本。
        """
        async with self._init_lock:
            if hasattr(self, "_initialized") and self._initialized:
                return
            self._initialized = True

            ctx = await self.lifecycle.emit(
                LifecycleHook.ON_INIT,
                tool_registry=self._tool_exec.registry,
                state=self.state,
            )

            # 插件可通过 system_prompt_append 追加内容到 system prompt
            append_text = ctx.data.get("system_prompt_append")
            if append_text:
                current = self._conv.system_prompt
                new_prompt = current + "\n\n" + append_text
                self._conv.set_system_prompt(new_prompt)

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
