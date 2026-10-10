"""MemoryPlugin — 记忆系统整体（读写工具 + 根索引注入 + 巩固机检）

记忆系统的唯一驻地（设计：/tmp/fp_memory_tree_design_20261009.md）：
  tree.py         树布局与派生缓存生成（_index.md / _node.md）
  index.py        SQLite FTS5 检索旁路（可丢弃缓存，损坏即重建）
  read.py/save.py memory_read / memory_save 两工具（定义 + 执行器）
  consolidate.py  巩固机检（split/merge/近重/孤儿/错抽屉，零 LLM）

钩子接线：
- ON_INIT(priority=70)
  ① 注册 memory_read/memory_save —— agent 私有 ToolRegistry（LLM 调用走这里）
     + fp_core.tools 全局单例（subagent 按名 execute_tool 走这里），双写幂等
  ② system_prompt_append 注入 level-0 根索引（读盘一次，排 task/zeta 之后
     落在 system prompt 末尾——设计：稳定内容在前、索引放最后、会话内冻结）
- ON_INITIALIZED：巩固机检（进程内一次；日门控/flock 在 run() 内）
- ON_BEFORE_COMMAND：重建前刷新索引段 —— 重读 _index.md（ensure_root
  顺带把 stale 派生缓存刷新），替换 state.prompt_appends 里自己那段；
  /clear /new /resume 重建即重放**新鲜**索引。会话中途不动 conv（保 prefix cache）。

文件即开关：
  plugins/memory → memory.disabled            整个记忆系统停用（工具+注入+机检）
  plugins/memory/consolidate.disabled 存在    仅停巩固机检（工具/注入不受影响）

刷新语义边界：终端命令路径全覆盖；api 会话操作（webui fork/switch）不经
ON_BEFORE_COMMAND，重放的是最近一次刷新的索引（次新，非最新）。
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any, cast

from fp_core.core.lifecycle import HookContext, LifecycleHook, LifecycleManager
from fp_core.logger import get_logger
from fp_core.plugins.base.plugin import Plugin, PluginConfig

from . import consolidate as memory_consolidate
from . import tree as memory_tree
from .read import PLUGIN_DEFINITION as READ_DEFINITION
from .read import execute as read_execute
from .save import PLUGIN_DEFINITION as SAVE_DEFINITION
from .save import execute as save_execute

if TYPE_CHECKING:
    from fp_core.core.state import State
    from fp_core.tools import ToolRegistry
    from fp_core.tools.core import OpenAISchema

__all__ = ["MemoryPlugin"]

# ON_INIT 优先级：晚于 task(50)/zeta(60) → 根索引排 system prompt 末尾
_INDEX_PRIORITY = 70
_TOOL_NAMES = ("memory_read", "memory_save")


class MemoryPlugin(Plugin):
    """记忆系统整体（工具注册 / 索引注入 / 巩固机检）"""

    name = "memory"
    version = "1.0.0"

    def __init__(self, config: PluginConfig | None = None):
        super().__init__(config)
        self._agent_state: State | None = None
        self._injected = False
        self._segment = ""  # 本插件在 prompt_appends 里的段（stripped，用于替换）
        self._consol_ran = False
        self._registries: list[ToolRegistry] = []

    # ── 插件生命周期 ────────────────────────────────────

    def on_register(self, lifecycle: LifecycleManager) -> None:
        lifecycle.register(
            LifecycleHook.ON_INIT,
            self._on_init,
            priority=_INDEX_PRIORITY,
            name="memory_init",
        )
        lifecycle.register(
            LifecycleHook.ON_INITIALIZED,
            self._on_initialized,
            priority=50,
            name="memory_consolidate_run",
        )
        lifecycle.register(
            LifecycleHook.ON_BEFORE_COMMAND,
            self._on_before_command,
            name="memory_index_refresh",
        )

    def on_unregister(self) -> None:
        """对称清理：注入过的注册表全部撤工具（残留工具可被 LLM 调用，见
        test_plugin_tool_cleanup 回归锚点）"""
        for reg in self._registries:
            for tool_name in _TOOL_NAMES:
                reg.unregister_tool(tool_name)
        self._registries.clear()
        self._agent_state = None
        self._injected = False
        self._segment = ""
        self._consol_ran = False

    # ── ON_INIT：工具注册 + 根索引注入 ──────────────────

    async def _on_init(self, ctx: HookContext, **kwargs: Any) -> HookContext:
        # ① 工具双写：agent 私有注册表（LLM 调用）+ 全局单例（subagent 按名调用）
        from fp_core.tools import registry as global_registry

        tool_registry: ToolRegistry | None = kwargs.get("tool_registry")
        for reg in (tool_registry, global_registry):
            if reg is None:
                continue
            if not any(id(r) == id(reg) for r in self._registries):
                self._registries.append(reg)
            self._register_tools(reg)

        # ② level-0 根索引注入（进程首读；之后由 ON_BEFORE_COMMAND 刷新）
        self._agent_state = kwargs.get("state")
        text = self._read_index()
        if text:
            ctx.data.setdefault("system_prompt_append", []).append(text)
        self._segment = text
        self._injected = True
        return ctx

    @staticmethod
    def _register_tools(reg: ToolRegistry) -> None:
        reg.register_tool("memory_read", cast("OpenAISchema", READ_DEFINITION), read_execute)
        reg.register_tool("memory_save", cast("OpenAISchema", SAVE_DEFINITION), save_execute)

    @staticmethod
    def _read_index() -> str:
        """读双根 level-0 索引（ensure_root 顺带刷新 stale 派生缓存）→ stripped 拼接"""
        parts = [p for p in (memory_tree.read_index("~"), memory_tree.read_index(".")) if p]
        return "\n".join(parts).strip()

    # ── ON_BEFORE_COMMAND：重建前刷新自己的注入段 ────────

    async def _on_before_command(self, ctx: HookContext, **kwargs: Any) -> HookContext:
        """命令（含 /clear /new /resume 等重建族）执行前重读索引并更新
        state.prompt_appends 中本插件的段 → 随后的 rebuild 重放新鲜索引。

        - 未注入（ON_INIT 未跑）→ 跳过：ensure_initialized 会整体覆盖
          prompt_appends，此刻写入必被冲掉
        - conv 系统提示词中途不碰（会话内冻结，保 prefix cache）
        - 成本：listdir+stat 扫描 + 两次小文件读，每次 slash 命令一次
        """
        if not self._injected or self._agent_state is None:
            return ctx
        new = self._read_index()
        if new == self._segment:
            return ctx
        appends = self._agent_state.prompt_appends
        if self._segment and self._segment in appends:
            appends[appends.index(self._segment)] = new
        elif new:
            appends.append(new)
        self._segment = new
        return ctx

    # ── ON_INITIALIZED：巩固机检 ────────────────────────

    async def _on_initialized(self, ctx: HookContext, **kwargs: Any) -> HookContext:
        if self._consol_ran:
            return ctx
        self._consol_ran = True
        # 子开关：仅停巩固机检（工具与注入不受影响）
        if os.path.exists(os.path.join(os.path.dirname(__file__), "consolidate.disabled")):
            return ctx
        log = get_logger()
        try:
            summary = await asyncio.to_thread(memory_consolidate.run)
            log.info(f"[memory] {summary}")
        except Exception as e:  # 检测失败绝不阻塞初始化
            log.warning(f"[memory] 巩固检测失败（不影响启动）: {e}")
        return ctx
