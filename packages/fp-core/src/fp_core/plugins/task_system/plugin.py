"""TaskSystemPlugin — 任务系统插件(任务图 / taskmap)

通过生命周期钩子注入任务图能力:
- ON_INIT: 注册 6 个任务工具 + 注入 system prompt 描述
- ON_BEFORE_LLM_CALL: 每次 LLM 调用前附加 [task] 提醒

定位: 任务 = 一张有向图(节点=状态, 边=结果语义)。它是长任务的**脊柱**:
目标、计划、进度、协作锁都活在这张图里。
"""

from __future__ import annotations

from typing import Any, cast

from fp_core.core.lifecycle import HookContext, LifecycleHook, LifecycleManager
from fp_core.plugins.base.plugin import Plugin, PluginConfig
from fp_core.taskmap.render import summary
from fp_core.taskmap.store import TaskMapStore
from fp_core.tools import ToolRegistry
from fp_core.tools.core import OpenAISchema

from . import command as task_command
from .tools import (
    ALL_DEFINITIONS,
    handle_clear,
    handle_create,
    handle_edit,
    handle_list,
    handle_read,
    handle_update,
)

# ── 注入 system prompt 的说明(解释信号与分工, 不重复工具 schema 的 description) ──

TASK_SYSTEM_DESCRIPTION = """【任务系统 · 任务图(taskmap)】
每个任务是一张**有向图**: 节点=须成立的状态, 边=带结果语义的转移。
- `task_create` 建任务(起点 n0 → 目标 n1); `task_read` 看全文; `task_edit` 改图结构;
  `task_update` 改元信息(整体状态/待澄清/引用); `task_list`/`task_clear`。
- **图会长大**: 从当前节点无法直达目标时, 用 `task_edit` 加中间节点(拆图);
  引入副作用(如"修驱动后蓝牙挂了")就新建一个 side_effect 节点, 修好再汇回主链。
- 边语义: complete(完美)/exhausted(N 次失败)/side_effect(副作用)/blocked/partial/requires_decompose(需拆图)。
- 提醒极简(`[task] ▶#id 节点 todo:n`): 只有状态 emoji/任务 id/当前节点 id/待办数;
  标题与节点描述**不在提醒里**, 要细节用 `task_read`/`task_list` 查。
  `⏸`=已交付待批准(停手汇报), `❗`=阻塞, `⏳`=暂停。

状态机: active(进行中) → delivered(已交付待批准, 必须停手等用户) → completed(用户批准);
用户推翻 → superseded(旧任务作废, 另建承接)。
终态(completed/superseded)可 `task_clear` 清除; delivered 不会被清除。"""


class TaskSystemPlugin(Plugin):
    """任务系统插件(任务图)"""

    name = "task_system"
    version = "2.0.0"

    def __init__(self, config: PluginConfig | None = None):
        super().__init__(config)
        self._store = TaskMapStore()
        self._tool_registry: ToolRegistry | None = None
        self._registered_tools: list[str] = []

    def on_register(self, lifecycle: LifecycleManager):
        """注册两个生命周期钩子"""
        lifecycle.register(LifecycleHook.ON_INIT, self._on_init, priority=50, name="task_system_init")
        lifecycle.register(
            LifecycleHook.ON_BEFORE_LLM_CALL,
            self._on_before_llm_call,
            priority=50,
            name="task_system_before_call",
        )

    def on_unregister(self):
        """插件卸载时清理: 清掉自己注入的任务工具与 /task 命令, 防止禁用后残留"""
        from fp_core.commands import unregister_command

        unregister_command("task")
        if self._tool_registry is not None:
            for tool_name in self._registered_tools:
                self._tool_registry.unregister_tool(tool_name)
        self._registered_tools.clear()
        self._store = TaskMapStore()

    # ── 钩子实现 ───────────────────────────────────

    async def _on_init(self, ctx: HookContext, **kwargs: Any) -> HookContext:
        """ON_INIT: 注册 6 个任务工具 + 注入 system prompt 描述"""
        tool_registry: ToolRegistry | None = kwargs.get("tool_registry")
        if tool_registry is not None:
            self._tool_registry = tool_registry
            for defn in ALL_DEFINITIONS:
                tool_name: str = defn["function"]["name"]
                executor = self._get_executor(tool_name)
                tool_registry.register_tool(tool_name, cast(OpenAISchema, defn), executor)
                if tool_name not in self._registered_tools:
                    self._registered_tools.append(tool_name)

        # 注入面向用户的 /task 控制台命令(与命令文件扫描地位相同)
        from fp_core.commands import register_command

        register_command("task", task_command)

        # list 收集正道: 不覆盖其他插件注入
        ctx.data.setdefault("system_prompt_append", []).append(TASK_SYSTEM_DESCRIPTION)
        return ctx

    async def _on_before_llm_call(self, ctx: HookContext, **kwargs: Any) -> HookContext:
        """ON_BEFORE_LLM_CALL: 追加 [task] 跟随提醒到最后一条消息末尾

        system prompt 描述**只走 ON_INIT 的 `system_prompt_append` 正道**（agent
        apply 进 _conv 后固化、随 resume 重放）。此处原有的兜底分支已删除：
        正常启动顺序是「先注册全部插件、再 ensure_initialized」，ON_INIT 必先执行，
        该分支实为死代码；且它写 modified_messages 只影响单次请求、不落盘，
        置位标志又在判断之外，触发也只闪现一次。
        """
        # 生成 [task] 提醒
        text = summary(self._store.list_all())
        if text is None:
            return ctx  # 无任务, 不附加

        hint = f"[task] {text}"

        modified = list(kwargs.get("messages", []))
        if not modified:
            return ctx
        last: dict[str, Any] = modified[-1]
        last_content: str | None = last.get("content")
        if not last_content:  # 纯 tool_calls 的 assistant 消息(content 为空)
            return ctx
        # 幂等：同一条末条消息不叠第二份 hint。
        # `modified_messages` 改语义后会写回 _conv 并落盘，重复注入会永久沉积；
        # 触发窗口 = 钩子 fire 后本轮夭折（LLM 报错 continue / 取消 / early-exit），
        # 无新消息落地、末条未换，下一次钩子又对着它。
        # 判据用稳定前缀而非 hint 全文——正文每轮都变（▶#1 n0 → n1）。
        # 正常每轮末条都会换（工具结果/新消息落地），故不影响「持续提醒」。
        if "\n\n[task] " in last_content:
            return ctx
        modified[-1] = dict(last)
        modified[-1]["content"] = last_content + f"\n\n{hint}"
        ctx.data["modified_messages"] = modified
        return ctx

    # ── 工具分派 ───────────────────────────────────

    def _get_executor(self, tool_name: str):
        executors = {
            "task_create": handle_create,
            "task_read": handle_read,
            "task_update": handle_update,
            "task_edit": handle_edit,
            "task_list": handle_list,
            "task_clear": handle_clear,
        }
        return executors[tool_name]
