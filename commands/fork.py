"""fork 命令 — 基于当前上下文新建会话"""

import display

name = "fork"
aliases = []
description = "基于当前上下文新建会话"


def execute(agent, arg: str) -> tuple[bool, str]:
    result = agent.fork()
    if result:
        display.info(result)
        return (True, result)
    display.info("当前会话没有消息，无法 fork")
    return (True, "")
