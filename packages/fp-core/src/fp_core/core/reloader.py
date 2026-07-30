"""
AgentReloader — 热重载引擎

在运行时刷新所有核心模块并重建 Agent 实例。
不关心具体前端（CLI / WebUI / ACP），只做纯粹的重载逻辑。

职责范围：
  1. 保存/恢复会话上下文
  2. 按依赖顺序 reload 模块（importlib）
  3. 重建 Agent 实例（含插件/工具重新发现）
  4. 提供 before/after 回调入口

不处理：
  - 前端通知（EventBus / WebSocket 等由调用方处理）
  - 重载期间请求排队/拒绝（调用方负责 is_processing 检查）
  - 外部插件实例的持久化（调用方传入需重新注册的插件）
"""

import importlib
import os
import sys
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from fp_core.logger import get_logger
from fp_core.plugins.base.plugin import Plugin

if TYPE_CHECKING:
    from fp_core.core.io import IOChannel

# ── 模块重载列表（按依赖顺序） ────────────────────────────────
# 第 1 层：无项目内部依赖
# 第 2 层：依赖 config
# 第 3 层：依赖 core.*
# 第 4 层：命令/工具注册表（含全局状态）
# 第 5 层：Agent 主干（依赖以上所有）

_RELOAD_MODULES: list[str] = [
    # Layer 1
    "fp_core.config",
    # Layer 2
    "fp_core.core.io",
    "fp_core.core.lifecycle",
    "fp_core.core.session",
    "fp_core.core.llm_client",
    # Layer 3
    "fp_core.plugins.base.plugin",
    "fp_core.prompts.agent",
    # Layer 4
    "fp_core.commands",
    "fp_core.tools",
    # Layer 5
    "fp_core.core.agent",
]


def _reload_modules() -> None:
    """按依赖顺序刷新所有核心模块。

    先 reload 各包的非主模块（子模块），
    再按 _RELOAD_MODULES 顺序 reload 主模块，
    确保父模块 import 时拿到的是已刷新的子模块代码。
    """
    reloaded_subs: set[str] = set()

    # ── 先 reload 子模块（排除主模块列表中的） ──
    for prefix in ("fp_core.tools.", "fp_core.commands.", "fp_core.core."):
        for mod_name in list(sys.modules.keys()):
            if (
                mod_name.startswith(prefix)
                and mod_name in sys.modules
                and mod_name not in _RELOAD_MODULES
                and mod_name not in reloaded_subs
            ):
                importlib.reload(sys.modules[mod_name])
                reloaded_subs.add(mod_name)

    # ── 按依赖顺序 reload 主模块 ──
    for mod_name in _RELOAD_MODULES:
        if mod_name in sys.modules:
            try:
                importlib.reload(sys.modules[mod_name])
            except Exception as e:
                raise RuntimeError(f"重载模块 {mod_name} 失败: {e}") from e

    importlib.invalidate_caches()


class AgentReloader:
    """
    Agent 热重载器

    纯逻辑，无 UI 绑定。CLI / WebUI / ACP 等前端均可调用。

    典型用法（CLI）::

        from fp_core.core.reloader import AgentReloader

        new_agent, info = await AgentReloader.reload(old_agent)
        # 替换全局引用
        agent = new_agent

    典型用法（WebUI）::

        new_agent, info = await AgentReloader.reload(
            old_agent,
            extra_plugins=[WebUIPlugin()],
            after_reload=lambda a: event_bus.publish({"type": "reload_done", ...}),
        )
        _agent = new_agent
    """

    @staticmethod
    def reload_modules() -> None:
        """仅刷新模块代码，不创建 Agent。

        适用于需要手动控制 Agent 创建时机的场景。
        """
        _reload_modules()

    @staticmethod
    async def reload(
        agent: Any,
        extra_plugins: list[Plugin] | None = None,
        io: "IOChannel | None" = None,
        on_shutdown: Any | None = None,
        before_reload: Callable[[], Awaitable[None] | None] | None = None,
        after_reload: Callable[[Any], Awaitable[None] | None] | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        """执行完整热重载流程。

        流程：
          1. before_reload 回调（调用方在此做前端通知）
          2. 保存旧会话上下文
          3. 关闭旧 Agent（释放连接池 + 清理钩子）
          4. importlib.reload 所有核心模块（含命令/工具重新发现）
          5. 创建新 Agent 实例（自动扫描内置+用户插件/工具）
          6. 注册 extra_plugins
          7. 触发 ON_INIT 生命周期
          8. 恢复旧会话上下文
          9. after_reload 回调

        Args:
            agent: 旧 Agent 实例
            extra_plugins: 需要额外注册的插件实例列表
            io: IO 通道实例。不传则新 Agent 使用默认静默 IOChannel()
            on_shutdown: shutdown 回调。不传则新 Agent 不使用关闭回调
            before_reload: 重载前回调（可做异步通知）
            after_reload: 重载完毕回调（接收新 Agent）

        Returns:
            (new_agent, info_dict)
            info_dict 包含 session_id / model / success

        Raises:
            RuntimeError: 模块重载或 Agent 创建失败
        """
        # ── 1. before_reload ──
        if before_reload is not None:
            result = before_reload()
            if result is not None and hasattr(result, "__await__"):
                await result

        # ── 2. 保存旧会话（上下文 + 摘要） ──
        old_sid: str | None = None
        if agent is not None:
            old_sid = agent.state.session.session_id
            agent.state.session.save_and_summarize(agent.state.conversation.to_serializable(), old_sid)

        # ── 3. 关闭旧 Agent（静默，不打印关闭面板） ──
        if agent is not None:
            agent.state.silent_shutdown = True
            await agent.shutdown()
            # 显式解除引用，帮助 GC 回收旧模块的对象
            agent = None

        # ── 4. 热重载模块 ──
        try:
            _reload_modules()
        except RuntimeError:
            get_logger().error("[AgentReloader] ❌ 模块重载失败")
            raise

        # ── 5. 创建新 Agent（静默，不打印 "📂 新会话"） ──
        # 必须重新 import 才能拿到重载后的类
        from fp_core.core.agent import Agent as _NewAgent  # type: ignore[import-untyped]

        _old_quiet = os.environ.get("FP_SUBAGENT_QUIET")
        os.environ["FP_SUBAGENT_QUIET"] = "1"
        try:
            new_agent = _NewAgent(enable_log=False, io=io, on_shutdown=on_shutdown)
        finally:
            if _old_quiet:
                os.environ["FP_SUBAGENT_QUIET"] = _old_quiet
            else:
                del os.environ["FP_SUBAGENT_QUIET"]

        # ── 6. 注册额外插件 ──
        if extra_plugins:
            for plugin in extra_plugins:
                try:
                    new_agent.plugins.register(plugin)
                except Exception as e:
                    get_logger().warning(f"[AgentReloader] ⚠️ 注册插件 {plugin.name} 失败: {e}")

        # ── 7. 初始化 ──
        try:
            await new_agent.ensure_initialized()
        except Exception as e:
            get_logger().error(f"[AgentReloader] ❌ 新 Agent 初始化失败: {e}")
            raise RuntimeError(f"新 Agent 初始化失败: {e}") from e

        # ── 8. 恢复旧会话 ──
        session_restored = False
        if old_sid:
            try:
                new_agent.state.session.switch_session(old_sid)
                from fp_core.core.prompt_builder import PromptBuilder

                prompt = PromptBuilder().build_system_prompt()
                new_agent.state.conversation.reset(prompt)
                saved = new_agent.state.session.load_context(prompt)
                if saved:
                    new_agent.state.conversation.replace_all(saved)
                session_restored = True
                get_logger().info(f"[AgentReloader] 🔄 已恢复会话: {old_sid}")
            except Exception as e:
                get_logger().warning(f"[AgentReloader] ⚠️ 会话恢复失败: {e}")

        # ── 9. after_reload ──
        if after_reload is not None:
            try:
                result = after_reload(new_agent)
                if result is not None and hasattr(result, "__await__"):
                    await result
            except Exception as e:
                get_logger().warning(f"[AgentReloader] ⚠️ after_reload 回调异常: {e}")

        info: dict[str, Any] = {
            "session_id": new_agent.state.session.session_id,
            "model": new_agent.model,
            "session_restored": session_restored,
        }

        get_logger().info(
            f"[AgentReloader] ✅ 重载完成 (model={new_agent.model}, session={new_agent.state.session.session_id})"
        )

        return new_agent, info
