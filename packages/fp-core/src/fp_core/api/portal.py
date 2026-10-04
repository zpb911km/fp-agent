"""Portal — 唯一接口组（外界 ↔ 核心 的唯一通信面）

协议文档：docs/dev/唯一接口组协议.md

两域分离（判据：实例不存在时该操作是否还有意义）：
    portal.run.* — 实例内的操作（对话流/命令/ask/只读状态），须先 ctl.open()
    portal.ctl.* — 控制实例的操作（生杀/热重启/会话管理/扩展注册），bootstrap() 无需实例
    portal.subscribe() — 出向事件流的唯一订阅点

设计约束：
    - 前端不得触碰 state/conversation/session 内部；跨边界只传协议数据类型
    - 会话重建、reload 续接消费、插件注入重放等样板在本层收编为用例 API

pyright: reportPrivateUsage=false —— run/ctl/sessions 等平面类是 Portal 的
实现细节（同模块内协作，共享 _agent/_active_io/_require）；对外边界是
「前端只导入 fp_core.api」，由 test_api_surface.py 的导入白名单强制。
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fp_core.api.types import (
    Event,
    ExitReason,
    InstanceNotOpenError,
    InstanceStatus,
    PortalError,
    ReloadDirective,
    SessionInfo,
    SessionMeta,
)
from fp_core.core.events import EventBridge, EventBus
from fp_core.core.io import IOChannel
from fp_core.core.messages import Response

if TYPE_CHECKING:
    from fp_core.core.agent import Agent
    from fp_core.core.lifecycle import LifecycleHook
    from fp_core.plugins.base.plugin import Plugin

__all__ = ["Portal", "Subscription", "portal"]


# ══════════════════════════════════════════════════════════════
# 事件订阅句柄
# ══════════════════════════════════════════════════════════════


@dataclass
class Subscription:
    """portal.subscribe() 返回的订阅句柄（cancel() 退订并停泵）"""

    sub_id: str
    _bus: EventBus
    _pump_task: asyncio.Task[None] | None = field(default=None, repr=False)

    def cancel(self) -> None:
        self._bus.unsubscribe(self.sub_id)
        if self._pump_task is not None:
            self._pump_task.cancel()
            self._pump_task = None


# ══════════════════════════════════════════════════════════════
# 实例内操作（run 域）
# ══════════════════════════════════════════════════════════════


class RunPlane:
    """实例内的操作 — 前置条件：portal.ctl.open() 已完成"""

    def __init__(self, portal: Portal) -> None:
        self._portal = portal

    # ── 对话流 ────────────────────────────────────────────────

    async def send(self, text: str, io: IOChannel | None = None) -> Response:
        """处理一轮用户输入（原 agent.process）"""
        agent = self._portal._require()
        if io is not None:
            self._portal._active_io = io  # 登记活跃通道，供 reply() 转发 ask 应答
        return await agent.process(text, io=io)

    async def continue_(self, io: IOChannel | None = None) -> Response:
        """续接上一轮未完成对话（仅供 reload handoff，契约见 ctl.take_reload）

        完成后内部清空 state._pending_continue —— 前端不再触碰该私有字段。
        """
        agent = self._portal._require()
        if io is not None:
            self._portal._active_io = io
        try:
            return await agent.continue_conversation(io=io)
        finally:
            agent.state._pending_continue = None  # pyright: ignore[reportPrivateUsage] 核内协议，由本层消化

    def cancel(self) -> None:
        """打断当前处理（原 agent.cancel）"""
        self._portal._require().cancel()

    # ── 斜杠命令 ──────────────────────────────────────────────

    async def command(self, line: str) -> tuple[bool, str]:
        """执行一条斜杠命令（原 agent.handle_command）"""
        return await self._portal._require().handle_command(line)

    # ── ask 带内应答 ─────────────────────────────────────────

    def reply(self, text: str, ask_id: str | None = None) -> bool:
        """注入 ask 回答（原 WebSocketIO.feed_reply 上提）。

        经当前活跃 IO 通道转发；无 pending ask / 无通道 → False
        （调用方按普通消息处理，语义与原 feed_reply 一致）。
        """
        io = self._portal._active_io
        if io is None:
            return False
        return io.reply(text, ask_id)

    # ── 只读状态 ──────────────────────────────────────────────

    @property
    def status(self) -> InstanceStatus:
        """实例状态只读快照（替代前端散落读 agent.state.*）"""
        agent = self._portal._require()
        return InstanceStatus(
            model=agent.model,
            session_id=agent.session.session_id,
            is_processing=agent.is_processing,
            cancelled=agent.cancelled_by_user,
        )

    @property
    def transcript(self) -> list[dict[str, Any]]:
        """当前会话消息只读快照（原 state.conversation.messages）"""
        return self._portal._require().state.conversation.messages

    @property
    def commands(self) -> dict[str, str]:
        """命令目录 {name: desc}（原 get_all_commands，无 '/' 前缀）"""
        from fp_core.commands import get_all_commands

        return get_all_commands()

    def reset_cancelled(self) -> None:
        """复位用户打断标记（原 agent.reset_cancelled）"""
        self._portal._require().reset_cancelled()

    # ── 唤醒 ─────────────────────────────────────────────────

    async def wake(self, io: IOChannel | None = None) -> Response | None:
        """唤醒实例消费待唤醒注入（如邻居铃声）——幂等、非阻塞。

        实例空闲且有 ``wake`` 级待注入事件时，起一轮消费它（不追加用户输入，
        环顶 drain 把注入作为 user 角色落入对话）；忙碌或无待唤醒事件则返回
        ``None``（不打断——人类优先）。空闲泵（portal 内部）与下游均可调用。
        """
        if not self._portal.is_open:
            return None
        agent = self._portal._require()
        from fp_core.core.jobs import has_pending_wake

        if agent.is_run_active or not has_pending_wake():
            return None
        return await agent.process_wakeup(io=io)


# ══════════════════════════════════════════════════════════════
# 控制实例操作（ctl 域）
# ══════════════════════════════════════════════════════════════


class ControlPlane:
    """控制实例的操作 — bootstrap() 无需实例，其余须实例已开启"""

    def __init__(self, portal: Portal) -> None:
        self._portal = portal

    # ── 生命周期 ──────────────────────────────────────────────

    def bootstrap(self) -> None:
        """入口第一行调用：捕获启动命令（原 handoff.capture_launch_command）"""
        from fp_core.core.handoff import capture_launch_command

        capture_launch_command()

    async def open(
        self,
        *,
        resume: str | None = None,
        session_id: str | None = None,
        io: IOChannel | None = None,
        role: Any | None = None,
        on_shutdown: Callable[..., Any] | None = None,
        plugins: Sequence[Plugin] = (),
        enable_log: bool = False,
    ) -> None:
        """创建并初始化实例（原 Agent(...) + 注册插件 + ensure_initialized 一体）

        Args:
            resume: 恢复指定会话（常用 os.environ.get("FP_RELOAD_SID")）
            session_id: 预分配会话 ID（subagent 用）
            io: 实例默认输出通道（前端实现的 IOChannel 子类）
            role: 角色定义（覆盖 system prompt/模型）
            on_shutdown: 关闭面板回调（终端用于渲染退出面板）
            plugins: 初始化前注册的扩展（须在 ON_INIT 前注册）
        """
        if self._portal._agent is not None:
            raise PortalError("实例已开启：请先 ctl.close() 再重新 open()")

        # 本地 re-import：确保热重载后拿到最新 Agent 类（热重启契约，勿提为模块级 import）
        from fp_core.core.agent import Agent as _AgentClass

        agent = _AgentClass(
            enable_log=enable_log,
            resume=resume,
            session_id=session_id,
            io=io,
            role=role,
            on_shutdown=on_shutdown,
        )
        # core 内建：lifecycle → 事件总线桥（原各前端自写桥接插件，归核统一）
        agent.plugins.register(EventBridge(self._portal.events))
        for plugin in plugins:
            agent.plugins.register(plugin)
        await agent.ensure_initialized()
        self._portal._agent = agent
        self._portal._start_wake_pump()

    async def close(self, *, reason: ExitReason = ExitReason.FINAL) -> None:
        """关闭实例（幂等：未开启时直接返回）

        reason 取代原 silent_shutdown/nuclear_exit 散落布尔：
            FINAL   — 保存+摘要+退出提示（正常退出）
            RECYCLE — 保存+摘要，静默（热切换：reload/重建旧实例）
            DISCARD — 不保存、删除会话文件，静默（不留痕退出）
        """
        agent = self._portal._agent
        if agent is None:
            return
        st = agent.state
        if reason is ExitReason.RECYCLE:
            st.silent_shutdown = True
        elif reason is ExitReason.DISCARD:
            st.nuclear_exit = True
            st.silent_shutdown = True
        try:
            await agent.shutdown()
        finally:
            await self._portal._stop_wake_pump()
            self._portal._agent = None
            self._portal._active_io = None

    # ── 热重启协议 ────────────────────────────────────────────

    def take_reload(self) -> ReloadDirective:
        """消费 reload handoff（原三份前端样板收编）

        内部消化 _reload_notice/_pending_continue 私有字段，返回结构化指令：
            d = portal.ctl.take_reload()
            if d.notice: 前端渲染 d.notice
            if d.should_continue: resp = await portal.run.continue_(io=...)

        前置：ctl.open() 已完成（handoff 消费需实例就绪）。
        """
        agent = self._portal._require()
        from fp_core.core.handoff import consume_reload_handoff

        should_continue = consume_reload_handoff(agent)
        st = agent.state
        notice = st._reload_notice  # pyright: ignore[reportPrivateUsage] 核内协议，由本层消化
        st._reload_notice = None  # pyright: ignore[reportPrivateUsage]
        return ReloadDirective(notice=notice, should_continue=should_continue)

    # ── 会话管理（原 8 处手写重建样板收编） ────────────────────

    @property
    def sessions(self) -> SessionCtl:
        return SessionCtl(self._portal)

    # ── 扩展点 ────────────────────────────────────────────────

    @property
    def hooks(self) -> HookCtl:
        return HookCtl(self._portal)

    @property
    def plugins(self) -> PluginCtl:
        return PluginCtl(self._portal)


# ══════════════════════════════════════════════════════════════
# ctl 子面：会话管理
# ══════════════════════════════════════════════════════════════


class SessionCtl:
    """会话操作（用例级：内部统一重建上下文 + 重放插件注入）"""

    def __init__(self, portal: Portal) -> None:
        self._portal = portal

    def new(self) -> SessionInfo:
        """新建空白会话（落盘当前 → 分配新 sid → 重建上下文）"""
        from fp_core.core.session_ops import fork_new

        return fork_new(self._portal._require().state)

    def load(self, sid: str) -> SessionInfo | None:
        """切换到历史会话；目标不存在返回 None（前端决定 404 或自动新建）"""
        from fp_core.core.session_ops import switch_to

        return switch_to(self._portal._require().state, sid)

    def clear(self) -> None:
        """清空当前会话文件并重建上下文"""
        from fp_core.core.session_ops import clear

        clear(self._portal._require().state)

    def list(self) -> dict[str, SessionMeta]:
        """全部会话元数据 {sid: meta}"""
        return self._portal._require().state.session.list_sessions()

    def delete(self, sid: str) -> bool:
        """删除指定会话文件"""
        return self._portal._require().state.session.delete_session(sid)

    def update_meta(self, sid: str | None = None, *, create: bool = True, **kwargs: Any) -> None:
        """更新会话 meta（sid 缺省 = 当前会话）

        `create=False` → 文件不存在时只改内存 meta、不落盘：标记类写入
        （subagent source、token_usage）不该给空会话凭空造 0 长度会话文件。
        """
        self._portal._require().state.session.update_meta(sid, create=create, **kwargs)

    def path(self, sid: str) -> str:
        """会话文件路径（供前端只读文件端点；文件不存在也返回路径）"""
        from fp_core.core.session import session_file_path

        return session_file_path(sid)

    def read_messages(self, sid: str) -> list[dict[str, Any]] | None:
        """只读解析会话消息（含 system；文件不存在 → None）"""
        from fp_core.core.session_ops import read_messages

        return read_messages(sid)


# ══════════════════════════════════════════════════════════════
# ctl 子面：扩展注册
# ══════════════════════════════════════════════════════════════


class HookCtl:
    """生命周期钩子注册（原前端直拿 agent.lifecycle 的收编面）"""

    def __init__(self, portal: Portal) -> None:
        self._portal = portal

    def register(
        self,
        hook: LifecycleHook,
        fn: Callable[..., Any],
        *,
        name: str,
        priority: int = 100,
    ) -> None:
        self._portal._require().lifecycle.register(hook, fn, priority=priority, name=name)

    def unregister(self, hook: LifecycleHook, name: str) -> bool:
        return self._portal._require().lifecycle.unregister(hook, name)


class PluginCtl:
    """插件注册（open(plugins=...) 为初始化前推荐路径；此面供运行期增删）"""

    def __init__(self, portal: Portal) -> None:
        self._portal = portal

    def register(self, plugin: Plugin) -> Plugin:
        return self._portal._require().plugins.register(plugin)

    def unregister(self, name: str) -> bool:
        return self._portal._require().plugins.unregister(name) is not None

    def get(self, name: str) -> Plugin | None:
        return self._portal._require().plugins.get(name)


# ══════════════════════════════════════════════════════════════
# Portal 根
# ══════════════════════════════════════════════════════════════


class Portal:
    """唯一接口组根对象（进程级单例，见 fp_core.api.portal）

    前端生命周期四步：
        portal.ctl.bootstrap()          # 入口第一行
        await portal.ctl.open(...)      # 创建实例
        ... portal.run.* / portal.ctl.sessions.* / portal.subscribe(...) ...
        await portal.ctl.close(...)     # 终结
    """

    def __init__(self) -> None:
        self.run = RunPlane(self)
        self.ctl = ControlPlane(self)
        self.events = EventBus()
        self._agent: Agent | None = None
        self._active_io: IOChannel | None = None
        # 空闲泵：实例空闲且有 wake 级注入时自动起一轮（见 run.wake）
        self._wake_pump_task: asyncio.Task[None] | None = None
        self._wake_pump_interval: float = 0.25

    # ── 状态 ──────────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        """实例是否已开启"""
        return self._agent is not None

    def _require(self) -> Agent:
        if self._agent is None:
            raise InstanceNotOpenError()
        return self._agent

    # ── 空闲泵（唤醒调度） ────────────────────────────────────

    def _start_wake_pump(self) -> None:
        """启动空闲泵（ctl.open 时）。幂等：已有存活泵则不重复起。"""
        if self._wake_pump_task is None or self._wake_pump_task.done():
            self._wake_pump_task = asyncio.create_task(self._wake_pump())

    async def _stop_wake_pump(self) -> None:
        """停止空闲泵（ctl.close 时）。"""
        task = self._wake_pump_task
        self._wake_pump_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _wake_pump(self) -> None:
        """空闲泵循环：周期检查待唤醒注入，空闲即消费（人类优先：忙碌跳过）。"""
        from fp_core.logger import get_logger

        log = get_logger()
        while True:
            await asyncio.sleep(self._wake_pump_interval)
            try:
                await self.run.wake()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 泵不得因单次失败而死
                log.warning(f"[wake-pump] 唤醒失败（忽略，下次重试）: {e}")

    # ── 事件订阅 ──────────────────────────────────────────────

    def subscribe(self, handler: Callable[[Event], Awaitable[None]]) -> Subscription:
        """订阅出向事件流（唯一订阅点）

        handler 逐事件被 await；cancel() 退订并停止泵任务。
        需要 seq/断连续传语义的传输层（webui ws）可直接使用 portal.events 的
        events_since/run_id/subscribe(queue) API。
        """
        sub_id, queue = self.events.subscribe()

        async def _pump() -> None:
            while True:
                event = await queue.get()
                await handler(event)

        task = asyncio.create_task(_pump(), name=f"portal_evt_{sub_id}")
        return Subscription(sub_id=sub_id, _bus=self.events, _pump_task=task)


# 进程级单例 —— 唯一接口组实例
portal = Portal()
