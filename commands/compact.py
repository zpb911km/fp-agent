"""compact 命令 — 压缩对话历史（异步）"""

import display


name = "compact"
aliases = []
description = "压缩对话历史"


async def execute(agent, arg: str) -> tuple[bool, str]:
    await agent._compact_context()
    return (True, "📦 历史已压缩")
