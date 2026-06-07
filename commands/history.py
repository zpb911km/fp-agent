"""history 命令 — 查看当前对话历史"""

import display


name = "history"
aliases = []
description = "查看当前对话历史"


def execute(agent, arg: str) -> tuple[bool, str]:
    agent._cmd_history()
    return (True, "📜 历史已显示（终端）")
