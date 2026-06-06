"""reset 命令 — 重置上下文（保留系统提示）"""

import display


name = "reset"
aliases = []
description = "重置上下文（保留系统提示）"


def execute(agent, arg: str) -> bool:
    agent._context.clear()
    agent._context.extend(agent._build_context())
    display.info("🔄 上下文已重置")
    return True
