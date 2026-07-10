"""compact 命令 — 压缩对话历史（异步）

直接调用 state.llm.summarize() + state.conversation.compact()，
不再经过 Agent.compact_context() 中转。
"""

from fp_core import display
from fp_core.core.conversation import CompactConfig

name = "compact"
aliases = []
description = "压缩对话历史"


async def execute(state, arg: str) -> tuple[bool, str]:
    history_count = state.conversation.get_non_system_count()
    if history_count <= 4:
        return (True, "对话历史较短，无需压缩")

    display.info("🔄 正在压缩对话历史...")

    async def summarizer(text: str) -> str:
        try:
            return await state.llm.summarize(text)
        except Exception as e:
            display.error(f" 压缩失败: {e}")
            return ""

    did_compact, msg = await state.conversation.compact(
        summarizer=summarizer,
        config=CompactConfig(keep_meaningful=4),
    )

    if did_compact:
        state.session.save_context(state.conversation.messages)
        display.info(f" ✅\n📦 {msg}")
    else:
        display.info(msg)

    return (True, "**📦 历史已压缩**")
