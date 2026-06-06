"""back 命令 — 回退到对话的某个历史时刻"""

import display


name = "back"
aliases = []
description = "回退到对话的某个历史时刻"


def execute(agent, arg: str) -> bool:
    agent._cmd_back()
    return True
