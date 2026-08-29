"""
ShortcircuitPlugin — 自我上下文修剪工具

通过 ON_INIT 获取 state + tool_registry，注册 shortcircuit 工具。
使 AI 能在上下文过长时主动压缩已完成的连通块。

全部逻辑通过公共 API + commands/shortcircuit 的纯函数实现。
"""

from collections.abc import Awaitable, Callable
from typing import Any, cast

from fp_core.commands import shortcircuit as _sc_impl
from fp_core.core.conversation import ConversationState
from fp_core.core.lifecycle import HookContext, LifecycleHook, LifecycleManager
from fp_core.core.state import State
from fp_core.plugins.base.plugin import Plugin, PluginConfig
from fp_core.tools import ToolRegistry
from fp_core.tools.core import OpenAISchema

# ── commands/shortcircuit 纯函数：经 getattr 注入精确类型（规避私有符号导入） ──
Component = dict[str, Any]
Refiner = Callable[[str, str, str], Awaitable[tuple[str, str]]]

_scan_components = cast(
    Callable[[list[dict[str, Any]]], list[Component]],
    _sc_impl._scan_components,  # type: ignore[reportPrivateUsage]
)
_degenerate = cast(
    Callable[
        [list[dict[str, Any]], list[tuple[int, int]], bool],
        tuple[bool, str, int, list[Component] | None],
    ],
    _sc_impl._degenerate,  # type: ignore[reportPrivateUsage]
)
_shortcircuit = cast(
    Callable[
        [list[dict[str, Any]], Refiner | None, list[tuple[int, int]], str],
        Awaitable[tuple[bool, str, int, list[Component] | None]],
    ],
    _sc_impl._shortcircuit,  # type: ignore[reportPrivateUsage]
)
_build_regenerate_refiner = cast(
    Callable[[State], Refiner],
    _sc_impl._build_regenerate_refiner,  # type: ignore[reportPrivateUsage]
)
_format_components_display = cast(
    Callable[[list[Component]], str],
    _sc_impl._format_components_display,  # type: ignore[reportPrivateUsage]
)

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "shortcircuit",
        "description": (
            "管理对话历史的连通块。默认对当前进行中的连通块执行退化(degenerate)："
            "删除块内的工具调用与工具返回消息，保留 AI 每一步输出的文本记录，"
            "把工具调用链退化为纯文本消息链，保持上下文简短且语义完整。"
            "也可对历史连通块执行合并压缩(crop/regenerate)。"
            "先用 action=list 查看概览，再按需处理。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["compress", "list"],
                    "description": "list=列出所有连通块, compress=执行退化或合并（默认）",
                    "default": "compress",
                },
                "count": {
                    "type": "integer",
                    "description": "从最晚的连通块开始处理 N 个。默认 1。互斥于 block_ids/range",
                    "default": 1,
                },
                "block_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "指定编号处理，如 [2,5] 表示 #2 和 #5。互斥于 count/range",
                },
                "range": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 2,
                    "maxItems": 2,
                    "description": "处理一个范围，如 [1,4] 表示 #1~#4 合并为一个块。互斥于 count/block_ids",
                },
                "mode": {
                    "type": "string",
                    "enum": ["degenerate", "crop", "regenerate"],
                    "description": (
                        "degenerate=退化（默认）：删除工具调用/返回消息，保留 AI 文本记录，"
                        "当前进行中的块永远安全，不破坏 agent 循环; "
                        "crop=合并删除工具消息; regenerate=合并并调 LLM 提炼"
                    ),
                    "default": "degenerate",
                },
            },
        },
    },
}


class ShortcircuitPlugin(Plugin):
    """自我上下文修剪插件"""

    name = "shortcircuit"
    version = "1.0.0"

    def __init__(self, config: PluginConfig | None = None):
        super().__init__(config)
        self._registered = False

    def on_register(self, lifecycle: LifecycleManager):
        lifecycle.register(
            LifecycleHook.ON_INIT,
            self._on_init,
            priority=100,
            name="shortcircuit_init",
        )

    def on_unregister(self):
        self._registered = False

    async def _on_init(self, ctx: HookContext, **kwargs: Any) -> HookContext:
        if self._registered:
            return ctx

        tool_registry: ToolRegistry | None = kwargs.get("tool_registry")
        state: State | None = kwargs.get("state")
        if tool_registry is None or state is None:
            return ctx

        registry: ToolRegistry = tool_registry
        st: State = state

        async def execute(params: dict[str, Any]) -> str:
            action: str = params.get("action", "compress")
            conv: ConversationState = st.conversation

            # ── 通过公共 API 获取非 system 消息 ──
            messages: list[dict[str, Any]] = conv.get_non_system_messages()

            # ── list：查看连通块概览 ──
            if action == "list":
                components: list[Component] = _scan_components(messages)
                return _format_components_display(components)

            # ── compress：执行退化或合并 ──
            mode: str = params.get("mode", "degenerate")
            block_ids: list[int] | None = params.get("block_ids")
            range_param: list[int] | None = params.get("range")
            count: int = params.get("count", 1)

            components = _scan_components(messages)
            if not components:
                return "没有连通块需要处理"

            # 确定要处理的目标（非 system 空间索引）
            # 过滤条件随模式变化：degenerate 看工具噪音（degenerable），合并看内容量（compressible）
            usable_key: str = "degenerable" if mode == "degenerate" else "compressible"

            target_raw: list[tuple[int, int]] = []

            if range_param is not None:
                start, end = range_param
                selected = [c for c in components if start <= c["idx"] <= end]
                if not selected:
                    return f"未找到编号 {start}~{end} 的连通块"
                min_user: int = selected[0]["user_idx"]
                max_terminal: int = selected[-1]["terminal_idx"]
                target_raw = [(min_user, max_terminal)]

            elif block_ids is not None:
                for bid in block_ids:
                    for comp in components:
                        if comp["idx"] == bid:
                            target_raw.append((comp["user_idx"], comp["terminal_idx"]))
                            break
                    else:
                        return f"未找到编号 {bid} 的连通块"

            else:
                # 默认：从最晚的连通块开始取前 count 个（与命令层一致）
                usable = [c for c in reversed(components) if c[usable_key]]
                if not usable:
                    if mode == "degenerate":
                        return "没有可退化的连通块（无工具调用噪音）"
                    return "所有连通块均已达最小状态（2 条消息），无需压缩"
                target_raw = [(c["user_idx"], c["terminal_idx"]) for c in usable[:count]]

            if not target_raw:
                return "没有可处理的连通块"

            # ── 执行（工具情境：保护调用点，保证 agent loop 不断开） ──
            if mode == "degenerate":
                success, msg, saved, new_messages = _degenerate(messages, target_raw, True)
            else:
                refiner: Refiner | None = None if mode == "crop" else _build_regenerate_refiner(st)
                success, msg, saved, new_messages = await _shortcircuit(messages, refiner, target_raw, mode)

            if success:
                # 通过公共 API 写回
                conv.set_messages(conv.system_prompt, cast(list[Component], new_messages))
                st.session.save_context(conv.to_serializable())
                if mode == "degenerate":
                    return f"已退化 {len(target_raw)} 个连通块，清理 {saved} 条工具消息"
                if range_param is not None:
                    return f"已合并 #{range_param[0]}~#{range_param[1]} 为一个连通块，节省 {saved} 条消息"
                return f"已压缩 {len(target_raw)} 个连通块，节省 {saved} 条消息"
            else:
                return f"处理失败: {msg}"

        registry.register_tool("shortcircuit", cast(OpenAISchema, TOOL_DEFINITION), execute)
        self._registered = True
        return ctx
