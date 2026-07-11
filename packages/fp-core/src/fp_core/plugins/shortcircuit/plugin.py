"""
ShortcircuitPlugin — 自我上下文修剪工具

通过 ON_INIT 获取 state + tool_registry，注册 shortcircuit 工具。
使 AI 能在上下文过长时主动压缩已完成的连通块。
"""

from fp_core.commands.shortcircuit import _build_regenerate_refiner, _format_components_display
from fp_core.core.lifecycle import HookContext, LifecycleHook, LifecycleManager
from fp_core.plugins.base.plugin import Plugin, PluginConfig

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "shortcircuit",
        "description": (
            "管理对话历史的连通块。用于对于已经完整完成的任务,保存其状态,消除其过程,主动节省上下文",
            "先用 action=list 查看概览，再用 action=compress 按需压缩。",
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["compress", "list"],
                    "description": "list=列出所有连通块, compress=执行压缩（默认）",
                    "default": "compress",
                },
                "count": {
                    "type": "integer",
                    "description": "从最晚的连通块开始压缩 N 个。默认 1。互斥于 block_ids/range",
                    "default": 1,
                },
                "block_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "指定编号压缩，如 [2,5] 表示 #2 和 #5。互斥于 count/range",
                },
                "range": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 2,
                    "maxItems": 2,
                    "description": "合并压缩一个范围，如 [1,4] 表示 #1~#4 合并为一条。互斥于 count/block_ids",
                },
                "mode": {
                    "type": "string",
                    "enum": ["crop", "regenerate"],
                    "description": "crop=只删工具消息不动回复（默认）, regenerate=调 LLM 提炼",
                    "default": "crop",
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

    async def _on_init(self, ctx: HookContext, **kwargs) -> HookContext:
        if self._registered:
            return ctx

        tool_registry = kwargs.get("tool_registry")
        state = kwargs.get("state")
        if tool_registry is None or state is None:
            return ctx

        async def execute(params: dict) -> str:
            action = params.get("action", "compress")
            conv = state.conversation

            # ── list：查看连通块概览 ──
            if action == "list":
                components = conv.scan_components()
                return _format_components_display(components)

            # ── compress：执行压缩 ──
            mode = params.get("mode", "crop")
            block_ids = params.get("block_ids")
            range_param = params.get("range")
            count = params.get("count", 1)

            components = conv.scan_components()
            if not components:
                return "没有连通块需要处理"

            # 确定要压缩的目标
            target_raw: list[tuple[int, int]] = []

            if range_param is not None:
                # 范围模式：合并为一个范围
                start, end = range_param
                selected = [c for c in components if start <= c["idx"] <= end]
                if not selected:
                    return f"未找到编号 {start}~{end} 的连通块"
                min_user = selected[0]["user_idx"]
                max_terminal = selected[-1]["terminal_idx"]
                target_raw = [(min_user, max_terminal)]

            elif block_ids is not None:
                # 指定编号模式
                for bid in block_ids:
                    for comp in components:
                        if comp["idx"] == bid:
                            target_raw.append((comp["user_idx"], comp["terminal_idx"]))
                            break
                    else:
                        return f"未找到编号 {bid} 的连通块"

            else:
                # count 模式：从最早的可压缩块开始
                compressible = [c for c in components if c["compressible"]]
                if not compressible:
                    return "所有连通块均已达最小状态（2 条消息），无需压缩"
                selected = compressible[:count]
                target_raw = [(c["user_idx"], c["terminal_idx"]) for c in selected]

            if not target_raw:
                return "没有可压缩的连通块"

            refiner = None if mode == "crop" else _build_regenerate_refiner(state)
            success, msg, saved = await conv.shortcircuit(refiner, target_raw, mode)

            if success:
                state.session.save_context(conv.to_serializable())
                if range_param is not None:
                    return f"已合并 #{range_param[0]}~#{range_param[1]} 为一个连通块，节省 {saved} 条消息"
                return f"已压缩 {len(target_raw)} 个连通块，节省 {saved} 条消息"
            else:
                return f"压缩失败: {msg}"

        tool_registry.register_tool("shortcircuit", TOOL_DEFINITION, execute)
        self._registered = True
        return ctx
