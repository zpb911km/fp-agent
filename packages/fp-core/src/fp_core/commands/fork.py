"""fork 命令 — 基于当前上下文新建会话"""

name = "fork"
aliases = []
description = "基于当前上下文新建会话"


def execute(agent, arg: str) -> tuple[bool, str]:
    result = agent.fork()
    if result:
        return (True, result)
    return (True, "当前会话没有消息，无法 `fork`")
