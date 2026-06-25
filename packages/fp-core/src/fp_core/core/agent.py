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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fp_core import config, display
from fp_core.commands import execute as execute_command
from fp_core.commands import get_all_commands
from fp_core.core import session
from fp_core.core.conversation import CompactConfig, ConversationState
from fp_core.core.io import CLIIO, IOChannel
from fp_core.core.lifecycle import HookContext, LifecycleHook, LifecycleManager
from fp_core.core.llm_service import LLMConfig, LLMService
from fp_core.core.prompt_builder import PromptBuilder
from fp_core.core.tool_executor import ToolExecutor
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
      - CLI 终端   → CLIIO（使用 input()）
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
    ):
        self.enable_log = enable_log

        # IO 通道（默认 CLI）
        self._default_io = io or CLIIO()

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

        # ConversationState：上下文状态的唯一所有者
        initial_prompt = self._prompter.build_system_prompt()
        self._conv = ConversationState(initial_prompt)

        # SessionManager：持久化（不再持有 _context）
        self.session = session.SessionManager(resume=resume)
        os.makedirs(config.SESSIONS_DIR, exist_ok=True)

        if not os.environ.get("FP_SUBAGENT_QUIET"):
            display.info(f"📂 新会话：{self.session.session_id}")

        # 从会话文件恢复历史
        saved = self.session.load_context(initial_prompt)
        if len(saved) > 1:
            self._conv.replace_all(saved)

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

        # 核弹退出标记
        self._nuclear_exit = False

        # 中断标记
        self._interrupted = False
        self._processing = False
        self._cancelled_by_user = False

        # 初始化锁（防竞态）
        self._init_lock = asyncio.Lock()

        # 内置钩子
        self._register_builtin_hooks()

        # 触发初始化生命周期
        # （init 在 _ensure_initialized 中触发）

    @property
    def _system_prompt(self) -> str:
        return self._conv.system_prompt

    @property
    def io(self) -> "IOChannel":
        """获取当前异步上下文的 IO 通道（context-local，防并发竞态）

        1. 优先返回 process() 的 context var 覆盖值（如 WebSocketIO）
        2. 无覆盖时退回到 __init__ 注入的默认通道（CLIIO）

        ⚠️  跨 asyncio.Task 隔离：
           contextvars 绑定到创建它的 Task，不会自动传播到子 Task。
           如果在子 Task 中访问此属性且父 Task 设置了 context var，
           将返回 None → fallback 到 CLIIO（而非预期通道）。
           见 _current_io 定义处的传播方案。
        """
        ctx_io: IOChannel | None = _current_io.get()
        return ctx_io if ctx_io is not None else self._default_io

    # ── 跨 Task context 传播工具 ─────────────────────

    @staticmethod
    def make_io_context() -> contextvars.Context:
        """捕获当前 asyncio Task 的 context 拷贝，用于子 Task 创建时传播 IO 通道。

        当需要在 _process_inner 内部创建子 Task 且子 Task 需要访问 agent.io 时，
        使用此方法获取 context 拷贝，确保 _current_io 在子 Task 中可见。

        用法：
          ctx = Agent.make_io_context()
          asyncio.create_task(ctx.run(some_async_fn()))

        Python 3.12+ 也可直接：
          asyncio.create_task(coro, context=Agent.make_io_context())

        原理：
          contextvars.copy_context() 捕获当前 Task 所有 ContextVar 的快照，
          ctx.run() 在指定 context 中执行代码，使子 Task 能读到正确的 _current_io。
        """
        return contextvars.copy_context()

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

    def _register_builtin_hooks(self):
        self.lifecycle.register(LifecycleHook.ON_INIT, self._on_init, priority=0, name="builtin_init")
        self.lifecycle.register(LifecycleHook.ON_SHUTDOWN, self._on_shutdown, priority=999, name="builtin_shutdown")

    async def _on_init(self, ctx: HookContext, **kwargs) -> HookContext:
        if self.enable_log:
            print("[Agent] Initializing...")
        ctx.data["initialized"] = True
        return ctx

    async def _on_shutdown(self, ctx: HookContext, **kwargs) -> HookContext:
        """关闭钩子 — 生成会话摘要 + 保存上下文 + 显示退出面板"""
        summary = ""
        if not self._nuclear_exit:
            last_msgs = self._conv.get_non_system_messages()
            if last_msgs:
                last_user = next((m for m in reversed(last_msgs) if m["role"] == "user"), None)
                if last_user:
                    summary = last_user.get("content", "").strip().replace("\n", " ")[:20]
                    self.session.update_meta(summary=summary)

        if not self._nuclear_exit:
            self.session.save_context(self._conv.messages)

        # 显示退出面板
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

        display.shutdown_panel(
            summary=summary,
            file=f"{self.session.session_id}.jsonl",
            model=self.model,
            msg_count=msg_count,
            created=created,
            duration=duration,
        )

        if self.enable_log:
            print("[Agent] Shutting down...")
        return ctx

    # ============ 会话管理 ============

    def switch_session(self, sid: str) -> bool:
        """切换会话"""
        if self.session.switch_session(sid):
            saved = self.session.load_context(self._conv.system_prompt)
            self._conv.replace_all(saved)
            return True
        return False

    def clear_session(self):
        """清空当前会话（重置上下文 + 清空会话文件）"""
        system_prompt = self._prompter.build_system_prompt()
        self._conv.reset(system_prompt)
        self.session.clear_session_file()

    def delete_session(self, sid: str, force: bool = False) -> bool:
        return self.session.delete_session(sid, force=force)

    # ============ 公共 API：上下文与持久化 ============

    def get_messages(self) -> list[dict]:
        """获取当前消息列表的防御性拷贝"""
        return self._conv.messages

    def save_context(self):
        """将当前上下文保存到会话文件"""
        self.session.save_context(self._conv.messages)

    def rebuild_context(self):
        """重建上下文：重新加载 system prompt + 从会话文件恢复"""
        prompt = self._prompter.build_system_prompt()
        self._conv.reset(prompt)
        saved = self.session.load_context(prompt)
        if len(saved) > 1:
            self._conv.replace_all(saved)

    async def ensure_initialized(self):
        """确保已触发初始化钩子（公共 API）"""
        await self._ensure_initialized()

    # ============ 中断机制 ============

    def cancel(self):
        """请求中断当前处理

        设置中断标记，在下一个循环检查点生效。
        注意：不会立即中断正在进行的 LLM 请求，
        请求完成后会检测标记并停止。
        跨线程安全（GIL 保护原子赋值）。
        """
        self._interrupted = True

    def _check_interrupted(self):
        """检查中断标记（纯实例方案）

        信号处理器通过 agent.cancel() 设置 self._interrupted，
        也可直接调用 cancel() 中断正在进行的处理。
        """
        if self._interrupted:
            self._interrupted = False
            self._processing = False
            raise asyncio.CancelledError("用户中断")

    # ============ 公共 API：会话管理 ============

    def resume_latest(self) -> bool:
        """续最新会话并重建上下文"""
        if self.session.resume_latest():
            saved = self.session.load_context(self._conv.system_prompt)
            self._conv.replace_all(saved)
            return True
        return False

    # ============ 公共 API：对话操作 ============

    async def back(self, target_idx: int | None = None, mode: int | None = None) -> str:
        """回退到对话的某个历史时刻（公共 API）

        Args:
            target_idx: 目标消息序号（1-based，从第一条非 system 开始），必填
            mode: 2 或 None=删除后续消息（默认），1=暂不支持

        Returns:
            状态描述文本
        """
        if target_idx is None:
            return "❌ 请指定要回退到的消息序号。使用 /back list 查看列表，/back <N> 直接回退"

        history_msgs = self._conv.get_history_for_display()

        if not history_msgs:
            return "没有历史记录可以回退"

        if target_idx < 1 or target_idx > len(history_msgs):
            return f"❌ 无效索引：{target_idx}，有效范围 1~{len(history_msgs)}"

        if mode == 1:
            return "❌ mode=1（保留后续消息）暂不支持，请使用 mode=2（删除后续消息）或 /fork"

        self._conv.back(target_idx=target_idx, mode=mode)
        self.session.save_context(self._conv.messages)

        if mode is None or mode == 2:
            return f"⏪ 已回退到第 {target_idx} 条消息，后续消息已删除"
        return "已回退"

    def get_history_for_display(self) -> list[dict]:
        """获取用于显示的历史消息列表（仅非 system 消息）"""
        return self._conv.get_history_for_display()

    def fork(self) -> str:
        """基于当前上下文新建会话（公共 API）

        Returns:
            fork 结果描述，空字符串表示无可 fork 内容
        """
        old_messages = self._conv.get_non_system_messages()

        if not old_messages:
            return ""

        self.session.save_context(self._conv.messages)

        last_msg_content = old_messages[-1].get("content", "")[:50] if old_messages else ""
        old_sid = self.session.session_id
        new_sid = self.session.create_session()

        # 重建上下文：用当前 system prompt，复制旧消息
        system_prompt = self._conv.system_prompt
        self._conv.reset(system_prompt)
        for m in old_messages:
            self._conv._messages.append(dict(m))
        self.session.save_context(self._conv.messages)

        # 更新旧会话摘要
        self.session.update_meta(old_sid, summary=last_msg_content)

        return f"🍴 已 fork：从 {old_sid} → {new_sid}"

    def history(self) -> list[dict]:
        """获取当前对话历史（仅非 system 消息）"""
        return self._conv.get_history_for_display()

    async def compact_context(self):
        """压缩对话历史（公共 API）"""
        await self._compact_context()

    # ═══════════════════════════════════════════════
    # Shortcircuit（短路）
    # ═══════════════════════════════════════════════

    def scan_components(self) -> list[dict]:
        """获取连通块列表（公共 API）"""
        return self._conv.scan_components()

    async def shortcircuit_context(
        self,
        count: int = 1,
        indices: list[int] | None = None,
        raw_indices: list[tuple[int, int]] | None = None,
        mode: str = "regenerate",
    ) -> dict:
        """短路上下文 — 将指定连通块压缩为 user/assistant 对

        Args:
            count:       短路最近 N 个可压缩态的连通块（默认 1）
            indices:     短路指定编号的连通块（@N 语法解析后的结果）
            raw_indices: 直接传入消息索引 [(user_idx, terminal_idx), ...]（合并范围用）
            mode:        "regenerate" | "crop"

        Returns:
            结构化结果 dict:
            {"ok": bool, "msg": str, "saved": int, "count": int}
            ok=False 时 msg 为失败原因
        """
        components = self._conv.scan_components()
        if not components:
            return {"ok": False, "msg": "没有已完成的连通块需要短路", "saved": 0, "count": 0}

        # 确定要短路的原始索引
        if raw_indices is not None:
            target_raw = list(raw_indices)
        elif indices is not None:
            target_raw: list[tuple[int, int]] = []
            for idx in indices:
                for comp in components:
                    if comp["idx"] == idx:
                        target_raw.append((comp["user_idx"], comp["terminal_idx"]))
                        break
        else:
            native = [c for c in reversed(components) if c["compressible"]]
            selected = native[:count]
            target_raw = [(c["user_idx"], c["terminal_idx"]) for c in selected]

        if not target_raw:
            return {"ok": False, "msg": "没有可短路的连通块，或指定的连通块编号不存在", "saved": 0, "count": 0}

        refiner = None if mode == "crop" else self._build_regenerate_refiner()
        success, msg, saved = await self._conv.shortcircuit(refiner, target_raw, mode)

        if success:
            self.session.save_context(self._conv.messages)
            return {"ok": True, "msg": msg, "saved": saved, "count": len(target_raw)}
        else:
            return {"ok": False, "msg": msg, "saved": 0, "count": 0}

    def _build_regenerate_refiner(self) -> Callable:
        """构建提炼回调 — 调用 LLM 精炼 assistant 回复"""

        async def refiner(user_text: str, assistant_text: str, context_text: str) -> tuple[str, str]:
            is_interrupted = assistant_text == "_INTERRUPTED_"
            prompt = (
                "请将以下完整对话进行重述。\n\n"
                "要求：\n"
                "1. 以第一人称描述整个过程的状态变化(我做了什么)\n"
                "2. 保留关键信息和持久化信息\n"
                "3. 只输出 AI 回复的内容，不要任何前缀或格式说明\n"
                "4. 适当概括, 适当保留信息, 提高信息密度\n"
                "5. 如果对话被中断（助手未完成回复），在末尾加上「（对话被中断）」"
            )
            try:
                result = await self._llm.summarize(
                    context_text,
                    instruction=prompt,
                    max_tokens=10000,
                )
                refined = (result or "").strip()
                if not refined:
                    refined = "被用户中断" if is_interrupted else assistant_text
            except Exception:
                refined = "被用户中断" if is_interrupted else assistant_text
            return (user_text, refined)

        return refiner

    def set_nuclear_exit(self):
        """设置核弹退出标志"""
        self._nuclear_exit = True

    # ============ LLM 调用（含 IO 展示） ============

    async def _invoke_llm(self, context: list[dict], silent: bool = False) -> dict:
        """发起聊天请求（含 spinner 和增量展示）

        实际 LLM 调用委托给 LLMService.chat()，
        此方法只负责 spinner/streamer 等 IO 展示。
        """
        spinner = None
        if not silent:
            spinner = display.Spinner("思考中")
            await spinner.start()

        try:
            assistant_msg = await self._llm.chat(context, tools=self._tool_exec.get_definitions())
        except asyncio.CancelledError:
            self._cancelled_by_user = True
            msg = {"role": "assistant", "content": "", "_interrupted": True}
            return msg
        finally:
            if spinner:
                await spinner.stop()

        reply_content = assistant_msg.get("content", "")

        streamer = display.LLMStreamer(silent=silent)
        if reply_content:
            streamer.write(reply_content)
        streamer.end()

        msg = {"role": "assistant", "content": reply_content}
        if assistant_msg.get("tool_calls"):
            msg["tool_calls"] = assistant_msg["tool_calls"]
        msg["_interrupted"] = False

        return msg

    async def _compact_context(self):
        """压缩上下文 — 委托给 ConversationState.compact() + LLM 摘要"""
        history_count = self._conv.get_non_system_count()
        if history_count <= 4:
            display.info("对话历史较短，无需压缩")
            return

        display.info("🔄 正在压缩对话历史...")

        async def summarizer(text: str) -> str:
            try:
                return await self._llm.summarize(text)
            except Exception as e:
                display.error(f" 压缩失败: {e}")
                return ""

        did_compact, msg = await self._conv.compact(summarizer=summarizer, config=CompactConfig(keep_meaningful=4))

        if did_compact:
            display.info(f" ✅\n📦 {msg}")
        else:
            display.info(msg)

    # ============ 工具展示 ============

    async def _execute_tool(self, tc: dict, silent: bool = False) -> str:
        """执行工具（含展示）"""
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"])
        except json.JSONDecodeError as e:
            return f"错误：工具参数 JSON 解析失败 - {e}"

        if not silent:
            safe_args = {k: str(v) for k, v in args.items()}
            display.llm_tool(f"  🛠️  {name}({json.dumps(safe_args, ensure_ascii=False)})")

        try:
            result = await self._tool_exec.execute(tc)
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:
            return f"❌ 工具执行失败 ({name}): {e}"

        if not silent:
            display.llm_tool(f"  📋  {result.strip()}")

        return result

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

        return await execute_command(self, cmd, arg)

    # ============ 主处理流程（全异步） ============

    async def process(self, user_input: str, io: IOChannel | None = None) -> Response:
        """
        处理用户输入（全异步）

        io: 可选 IO 通道覆盖。WebUI 模式传入 WebSocketIO
           io=None → 使用 self._default_io（CLIIO）

        context var 生命周期：
          _current_io.set(io or self._default_io) 在此方法入口调用 → 绑定到当前 asyncio.Task
          _current_io.reset(token) 在 finally 块中恢复 → 保证不泄漏

          注意：_current_io 只对当前 Task 可见。
          如果在 _process_inner 内部创建子 Task，需手动传播 context，
          见 _current_io 定义处的说明。
        """
        await self._ensure_initialized()

        if not user_input.strip():
            return Response(content="")

        # 使用 contextvars 设置 IO 通道（不修改实例变量，防并发竞态）
        # io=None → fallback 到 self._default_io，保证 get_current_io() 始终返回有效值
        token = _current_io.set(io or self._default_io)
        try:
            return await self._process_inner(user_input)
        finally:
            _current_io.reset(token)

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

            messages_for_llm = ctx.data.get("modified_messages", self._conv.messages)

            self._processing = True
            try:
                assistant_msg = await self._invoke_llm(messages_for_llm, silent=_silent)
            except (asyncio.CancelledError, KeyboardInterrupt):
                self._processing = False
                raise
            except Exception as e:
                self._processing = False
                display.error(f"API/LLM 错误: {e}")
                await self.lifecycle.emit(LifecycleHook.ON_ERROR, error=str(e))
                err_str = str(e)
                if "'tool'" in err_str and "preceding" in err_str:
                    display.warning("  🔧 检测到 tool 顺序错误，二次修复...")
                    self._conv.repair_tool_ordering()
                    try:
                        self._processing = True
                        assistant_msg = await self._invoke_llm(self._conv.messages, silent=_silent)
                    except (asyncio.CancelledError, KeyboardInterrupt):
                        self._processing = False
                        raise
                    except Exception as e2:
                        self._processing = False
                        display.error(f"  ❌ 修复后仍失败: {e2}")
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
                display.info("⏹️ 已中断（保留了已生成的内容）")
                break

            # ── 处理工具调用 ──
            tool_calls = assistant_msg.get("tool_calls", [])
            tool_interrupted = False
            if tool_calls:
                sel_tool_names = [tc["function"]["name"] for tc in tool_calls]
                ctx = await self.lifecycle.emit(LifecycleHook.ON_TOOL_SELECT, tools=sel_tool_names)
                if ctx.data.get("cancelled"):
                    display.warning(f"⏹️ 工具执行被插件拦截: {ctx.data.get('cancel_reason', '无原因')}")
                    break
                # 如果插件修改了工具列表，按新列表过滤
                modified_tools = ctx.data.get("modified_tools")
                if modified_tools is not None:
                    skipped = [
                        tc["function"]["name"] for tc in tool_calls if tc["function"]["name"] not in modified_tools
                    ]
                    if skipped:
                        display.info(f"🔧 插件过滤了工具: {', '.join(skipped)}")
                    tool_calls = [tc for tc in tool_calls if tc["function"]["name"] in modified_tools]

                for i, tc in enumerate(tool_calls):
                    try:
                        ctx = await self.lifecycle.emit(
                            LifecycleHook.ON_TOOL_CALL,
                            tool_name=tc["function"]["name"],
                            tool_args=tc["function"]["arguments"][:5000],
                            tool_call_id=tc["id"],
                        )
                        if ctx.data.get("cancelled"):
                            reason = ctx.data.get("cancel_reason", "工具被插件拒绝")
                            display.info(f"🔧 插件拒绝工具 {tc['function']['name']}: {reason}")
                            # 构造假结果，走完整的结果处理流程
                            result = "被用户拒绝"
                            ctx = await self.lifecycle.emit(
                                LifecycleHook.ON_TOOL_RESULT,
                                tool_name=tc["function"]["name"],
                                result=result,
                                tool_call_id=tc["id"],
                            )
                            if ctx.data.get("blocked"):
                                result = ctx.data.get("block_reason", "结果被插件过滤")
                            modified_result = ctx.data.get("modified_result")
                            if modified_result is not None:
                                result = modified_result
                            self._conv.add_tool_message(tc["id"], result)
                            continue
                        # 使用插件修改后的参数
                        modified_args = ctx.data.get("modified_tool_args")
                        if modified_args is not None:
                            tc["function"]["arguments"] = modified_args
                            display.info(f"🔧 插件修改了 {tc['function']['name']} 的参数")

                        result = await self._execute_tool(tc, silent=_silent)

                        ctx = await self.lifecycle.emit(
                            LifecycleHook.ON_TOOL_RESULT,
                            tool_name=tc["function"]["name"],
                            result=result[:5000],
                            tool_call_id=tc["id"],
                        )
                        if ctx.data.get("blocked"):
                            display.warning(f"🔧 工具 {tc['function']['name']} 的结果被插件过滤")
                            result = ctx.data.get("block_reason", "结果被插件过滤")
                        modified_result = ctx.data.get("modified_result")
                        if modified_result is not None:
                            result = modified_result
                            display.info(f"🔧 插件修改了 {tc['function']['name']} 的结果")

                        self._conv.add_tool_message(tc["id"], result)

                    except (KeyboardInterrupt, asyncio.CancelledError):
                        self._interrupted = False
                        self._cancelled_by_user = True
                        self._conv.add_tool_message(tc["id"], "工具调用失败：用户中断")
                        for remaining in tool_calls[i + 1 :]:
                            self._conv.add_tool_message(remaining["id"], "未执行（工具调用失败：用户中断）")
                        display.info("⏹️ 已中断（上下文已保留工具调用信息）")
                        tool_interrupted = True
                        break
                    except Exception as e:
                        # 工具执行异常 → 通知插件，看是否可抑制
                        display.error(f"工具 {tc['function']['name']} 执行错误: {e}")
                        ctx = await self.lifecycle.emit(
                            LifecycleHook.ON_TOOL_ERROR,
                            tool_name=tc["function"]["name"],
                            error=str(e),
                            tool_call_id=tc["id"],
                        )
                        if ctx.data.get("suppressed"):
                            reason = ctx.data.get("suppress_reason", "插件已抑制错误")
                            display.warning(f"  ⚠️ 错误已被插件抑制: {reason}")
                            self._conv.add_tool_message(tc["id"], f"错误已被抑制：{reason}")
                        else:
                            raise

                if tool_interrupted:
                    break
                continue
            else:
                break

        # ── 生命周期：上下文已更新 ──
        await self.lifecycle.emit(
            LifecycleHook.ON_CONTEXT_UPDATE,
            msg_count=len(self._conv),
        )

        # 保存上下文
        self.session.save_context(self._conv.messages)

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

    async def _ensure_initialized(self):
        """确保已触发初始化钩子（线程安全，asyncio.Lock 保护）"""
        async with self._init_lock:
            if hasattr(self, "_initialized") and self._initialized:
                return
            self._initialized = True
            await self.lifecycle.emit(LifecycleHook.ON_INIT)

    async def shutdown(self):
        """关闭 Agent（清理生命周期钩子 + 释放连接池）"""
        await self.lifecycle.emit(LifecycleHook.ON_SHUTDOWN)
        await self.lifecycle.emit(LifecycleHook.ON_CLEANUP)

        # 清理所有生命周期钩子（防止热重载后旧钩子残留）
        self.lifecycle.clear()

        # 关闭 LLM client 连接池（httpx.AsyncClient）
        with contextlib.suppress(Exception):
            await self.client.close()

        # _on_shutdown 钩子已保存上下文，此处只需处理核弹模式
        if self._nuclear_exit:
            self.session.delete_session(self.session.session_id, force=True)
            display.info("💥 核弹模式：当前会话已删除，不留痕迹")

        display.info("👋 Agent 已关闭")
