"""reset 命令 — 重置上下文（保留系统提示）"""

import display


name = "reset"
aliases = []
description = "重置上下文（保留系统提示）"


def execute(agent, arg: str) -> tuple[bool, str]:
    agent.clear_session()
    msg = "🔄 上下文已重置"
    display.info(msg)
    return (True, msg)
