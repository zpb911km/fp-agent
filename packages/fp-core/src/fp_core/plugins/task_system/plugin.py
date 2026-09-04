"""TaskSystemPlugin — 任务系统插件

通过生命周期钩子注入任务管理能力：
- ON_INIT: 注册 4 个任务工具 + 注入 system prompt 描述
- ON_BEFORE_LLM_CALL: 每次 LLM 调用前附加 [task] 提醒
"""

from typing import Any, cast

from fp_core.core.lifecycle import HookContext, LifecycleHook, LifecycleManager
from fp_core.plugins.base.plugin import Plugin, PluginConfig
from fp_core.tools import ToolRegistry
from fp_core.tools.core import OpenAISchema

from .store import TaskStore
from .tools import ALL_DEFINITIONS, handle_clear, handle_create, handle_list, handle_update

# ── Task 系统描述文本（注入到 system prompt） ────────
# 注意：不重复工具功能（已通过 OpenAI schema 的 description 提供）。
# 此处只解释消息中 [task] 标记的含义，让 LLM 理解这个信号。

TASK_SYSTEM_DESCRIPTION = """【任务系统】
每次 LLM 调用前会在消息末尾看到 [task] 进度标记：
  [task] ▶#N          — 进行中的任务 #N
  [task] ⏸#N 待批准   — 任务 #N 已交付，停下汇报，等用户批准后再置 completed
  [task] ⬜M           — M 个待办任务
  [task] ▶#N ⬜M      — 进行中 #N + 待办 M 个
无标记 = 无待办任务

看到 [task] 时，根据需要调用 task_create/task_update/task_clear 管理进度。
通过 task_list 查看完整清单，使用 #ID 引用追踪任务。

状态机：
  pending(待办) → in_progress(进行中) → delivered(已交付待批准) → completed(已完成)
  用户推翻/要求重写时：旧任务置 superseded(已作废)，再 task_create 新建任务承接；
  delivered 状态下必须停手等用户表态，不得自行推进下一步。
终态（completed/superseded）可 task_clear 清除；delivered 不会被清除。"""


class TaskSystemPlugin(Plugin):
    """任务系统插件"""

    name = "task_system"
    version = "1.0.0"

    def __init__(self, config: PluginConfig | None = None):
        super().__init__(config)
        self._store = TaskStore()
        self._description_injected = False
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
        """插件卸载时清理：清掉自己注入的任务工具，防止禁用后残留"""
        if self._tool_registry is not None:
            for tool_name in self._registered_tools:
                self._tool_registry.unregister_tool(tool_name)
        self._registered_tools.clear()
        self._store = TaskStore()

    # ── 钩子实现 ───────────────────────────────────

    async def _on_init(self, ctx: HookContext, **kwargs: Any) -> HookContext:
        """ON_INIT 钩子：注册工具 + 注入 system prompt 描述

        从 kwargs 中获取 tool_registry，注册 4 个任务工具。
        通过 context.data.system_prompt_append 返回描述文本，
        由 Agent 在 ON_INIT emit 后追加到 system prompt。
        """
        # 1. 注册工具（记录清单，on_unregister 成对清理）
        tool_registry: ToolRegistry | None = kwargs.get("tool_registry")
        if tool_registry is not None:
            self._tool_registry = tool_registry
            for defn in ALL_DEFINITIONS:
                tool_name: str = defn["function"]["name"]
                executor = self._get_executor(tool_name)
                tool_registry.register_tool(tool_name, cast(OpenAISchema, defn), executor)
                if tool_name not in self._registered_tools:
                    self._registered_tools.append(tool_name)

        # 2. 标记描述已注入（ON_BEFORE_LLM_CALL 不再重复注入）
        self._description_injected = True

        # 3. 返回 system_prompt_append（list 收集正道：不覆盖其他插件注入）
        #    通过 HookContext.data 传递，Agent 在 emit 后读取并追加到 system prompt
        ctx.data.setdefault("system_prompt_append", []).append(TASK_SYSTEM_DESCRIPTION)

        return ctx

    async def _on_before_llm_call(self, ctx: HookContext, **kwargs: Any) -> HookContext:
        """ON_BEFORE_LLM_CALL 钩子：附加 [task] 跟随提醒

        将 [task] 标记追加到最后一条消息的 content 末尾，格式：
          [task] ▶#N ⬜M
          [task] ▶#N
          [task] ⬜M
        无待办/进行中时不附加。

        如果最后一条消息无 content（如纯 tool_calls 的 assistant 消息），跳过不附加。
        """
        # 首次调用时注入 system prompt 描述（如果 ON_INIT 没完成注入的 fallback）
        if not self._description_injected:
            modified: list[dict[str, Any]] = list(kwargs.get("messages", []))
            if modified and modified[0].get("role") == "system":
                existing: str = modified[0]["content"]
                if "【任务系统】" not in existing:
                    modified[0] = dict(modified[0])
                    modified[0]["content"] = existing + "\n\n" + TASK_SYSTEM_DESCRIPTION
                    ctx.data["modified_messages"] = modified
            self._description_injected = True

        # 生成 [task] 提醒
        summary = self._store.summarize()
        if summary is None:
            return ctx  # 无任务，不附加

        hint = f"[task] {summary}"

        # ── 将 [task] 追加到最后一条消息的 content 末尾 ──
        modified: list[dict[str, Any]] = list(kwargs.get("messages", []))
        if not modified:
            return ctx  # 空消息列表，跳过

        last: dict[str, Any] = modified[-1]
        last_content: str | None = last.get("content")
        if not last_content:  # content 为 None 或空字符串（如纯 tool_calls 的 assistant 消息）
            return ctx  # 跳过，不附加

        modified[-1] = dict(last)  # 复制一份避免副作用
        modified[-1]["content"] = last_content + f"\n\n{hint}"
        ctx.data["modified_messages"] = modified

        return ctx

    # ── 工具分派 ───────────────────────────────────

    def _get_executor(self, tool_name: str):
        """根据工具名返回对应的异步处理函数"""
        executors = {
            "task_create": handle_create,
            "task_update": handle_update,
            "task_list": handle_list,
            "task_clear": handle_clear,
        }
        return executors[tool_name]
