"""
ShortcircuitPlugin — 自我上下文修剪插件

通过 ON_INIT 钩子：
  1. 注册 shortcircuit 工具（action 缺省 list；当前块强制 degenerate）
  2. 注入 /sc 命令（与工具同一 apply_action，语义完全一致）

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
from .core import apply_action

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "shortcircuit",
        "description": (
            "修剪对话历史以省上下文。块=一条用户消息到下一次用户消息之间的记录。"
            "action：list(默认，只看不改)｜degenerate(文字全留，段内汇成一对)｜"
            "crop(只留首问+末答，中间全丢)｜regenerate(LLM 提炼)。"
            'target（action≠list 时必填）："#7" 单块；"#3-9" 当一个整体合成一对；'
            '"#3,#7-9" 逗号分段，各段分别处理。当前块（最后一个）只能 degenerate。'
            "返回「消息 A→B 条」是实测对账：A==B 即没剪到东西。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": '"#7" 单块｜"#3-9" 一个整体｜"#3,#7-9" 多段分别处理',
                },
                "action": {
                    "type": "string",
                    "enum": ["list", "degenerate", "crop", "regenerate"],
                    "description": "默认 list（只列不改）",
                    "default": "list",
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
            action: str = params.get("action", "list")
            target: str = params.get("target") or ""
            conv: ConversationState = st.conversation
            messages: list[dict[str, Any]] = conv.get_non_system_messages()
            # 工具与命令共用 apply_action：语义唯一，写回也在其中完成
            _ok, msg = await apply_action(messages, st, action, target, protect_callsite=True)
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
