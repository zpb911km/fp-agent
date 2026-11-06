"""
ShortcircuitPlugin — 自我上下文修剪插件

通过 ON_INIT 钩子：
  1. 注册 shortcircuit 工具（工具层硬性策略：当前块强制退化、其他块默认 crop）
  2. 注入 /sc 命令（命令层无硬性约束，自由处理任意块）

全部逻辑位于同包 core.py（纯函数），本模块只做注册与装配。
"""

from typing import Any, cast

from fp_core.commands import get_command, register_command, unregister_command
from fp_core.core.conversation import ConversationState
from fp_core.core.lifecycle import HookContext, LifecycleHook, LifecycleManager
from fp_core.plugins.base.plugin import Plugin, PluginConfig
from fp_core.tools import ToolRegistry
from fp_core.tools.core import OpenAISchema

from . import command as sc_command
from .core import execute_plan, format_components_display, scan_components

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "shortcircuit",
        "description": (
            "修剪对话历史的连通块（块=一条用户消息到下一次用户消息之间的记录）。"
            "mode：degenerate=删工具调用/返回、留 AI 文本链；"
            "crop=只留首条 user 与末条 assistant、中间全丢；regenerate=LLM 提炼。"
            "硬性：当前块（最后一个）只能 degenerate；其他块未指定 mode 时按 crop。"
            "选块：count=最近 N 个可处理块；block_ids=[2,5]=逐个处理 #2 #5；"
            "range=[1,4]=把 #1~#4 当一个跨度（crop 即首块请求+末块回复，非逐块）。"
            "返回的「消息 A→B 条」是实测对账：A=B 即没剪到东西，要换块或换 mode；"
            "action=list 看各块编号/状态/条数。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["compress", "list"],
                    "description": "list=列出连通块（编号/状态/条数）, compress=执行修剪（默认）",
                    "default": "compress",
                },
                "count": {
                    "type": "integer",
                    "description": "最近 N 个可处理块，默认 1。互斥于 block_ids/range",
                    "default": 1,
                },
                "block_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "逐个处理这些编号，如 [2,5] → #2、#5 各处理一次。互斥于 count/range",
                },
                "range": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 2,
                    "maxItems": 2,
                    "description": "把 #start~#end 当作一个跨度处理（非逐块）。互斥于 count/block_ids",
                },
                "mode": {
                    "type": "string",
                    "enum": ["degenerate", "crop", "regenerate"],
                    "description": ("不传=当前块退化+其他块 crop；显式指定只作用于其他块（当前块永远退化）"),
                },
            },
        },
    },
}


class ShortcircuitPlugin(Plugin):
    """自我上下文修剪插件（工具 + /sc 命令注入）"""

    name = "shortcircuit"
    version = "1.0.0"

    def __init__(self, config: PluginConfig | None = None):
        super().__init__(config)
        self._registered = False
        self._command_injected = False
        self._tool_registry: ToolRegistry | None = None

    def on_register(self, lifecycle: LifecycleManager):
        lifecycle.register(
            LifecycleHook.ON_INIT,
            self._on_init,
            priority=100,
            name="shortcircuit_init",
        )

    def on_unregister(self):
        if self._tool_registry is not None and self._registered:
            # 工具/命令由 ON_INIT 注入，但 on_unregister 可能先于 Agent 重建
            # 被调用（PluginRegistry.unregister → plugin.on_unregister）。
            # 必须成对清理：命令否则 /sc 残留，工具否则 shortcircuit 仍可被 LLM 调用。
            self._tool_registry.unregister_tool("shortcircuit")
        if self._command_injected:
            unregister_command("sc")
            self._command_injected = False
        self._registered = False

    async def _on_init(self, ctx: HookContext, **kwargs: Any) -> HookContext:
        if self._registered:
            return ctx

        tool_registry: ToolRegistry | None = kwargs.get("tool_registry")
        # state 为动态对象（fp_core 无 py.typed，State 在 pyright 中解析为 Unknown），
        # 一律用 Any 兜底避免 Unknown 级联
        state: Any | None = kwargs.get("state")
        if tool_registry is None or state is None:
            return ctx

        registry: ToolRegistry = tool_registry
        st: Any = state
        self._tool_registry = registry

        async def execute(params: dict[str, Any]) -> str:
            action: str = params.get("action", "compress")
            conv: ConversationState = st.conversation

            # ── 通过公共 API 获取非 system 消息 ──
            messages: list[dict[str, Any]] = conv.get_non_system_messages()

            # ── list：查看连通块概览 ──
            if action == "list":
                components = scan_components(messages)
                return format_components_display(components)

            # ── compress：执行退化或合并（统一策略：当前块强制退化，其他块默认 crop） ──
            mode: str | None = params.get("mode")
            mode_explicit = "mode" in params
            block_ids: list[int] | None = params.get("block_ids")
            range_param: list[int] | None = params.get("range")
            count: int = params.get("count", 1)

            # 目标选择过滤字段：显式 crop/regenerate 看内容量（compressible），
            # 其余（未指定或显式 degenerate）看工具噪音（degenerable）——保持工具层
            # 默认意图「优先处理含工具噪音的块（通常是当前进行中的块）」。
            usable_key: str = "degenerable" if (not mode_explicit or mode == "degenerate") else "compressible"

            if range_param is not None:
                plan_action: str = "range"
                plan_value: Any = (range_param[0], range_param[1])
            elif block_ids is not None:
                plan_action = "indices"
                plan_value = block_ids
            else:
                plan_action = "count"
                plan_value = count

            success, msg, _saved, new_messages = await execute_plan(
                messages, st, plan_action, plan_value, mode, usable_key, True
            )

            if not success:
                return f"处理失败: {msg}"

            # 通过公共 API 写回
            assert new_messages is not None
            conv.set_messages(conv.system_prompt, new_messages)
            st.session.save_context(conv.to_serializable())
            # execute_plan 已返回完整对账行（动作+编号+条数变化+剩余结构），不再二次包装
            return msg

        registry.register_tool("shortcircuit", cast(OpenAISchema, TOOL_DEFINITION), execute)
        self._registered = True

        # ── 注入 /sc 命令（插件入口注入，不走自动发现） ──
        # reload 重建 Agent 后 ON_INIT 重跑：若注册表里仍是本插件旧模块对象则无需重复注入
        if not self._command_injected:
            existing = get_command("sc")
            if existing is not sc_command:
                register_command("sc", sc_command)
            self._command_injected = True
        return ctx
