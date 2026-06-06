"""back 命令 — 回退到对话的某个历史时刻（异步）"""

import display


name = "back"
aliases = []
description = "回退到对话的某个历史时刻"


async def execute(agent, arg: str) -> bool:
    await agent._cmd_back()
    return True
