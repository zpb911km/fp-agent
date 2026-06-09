"""history 命令 — 查看当前对话历史"""


name = "history"
aliases = []
description = "查看当前对话历史"


def execute(agent, arg: str) -> tuple[bool, str]:
    agent.history()
    return (True, "📜 历史已显示（终端）")
