"""fork 命令 — 基于当前上下文新建会话"""

import display


name = "fork"
aliases = []
description = "基于当前上下文新建会话"


def execute(agent, arg: str) -> tuple[bool, str]:
    agent._cmd_fork()
    return (True, "🍴 已 fork 新会话")
