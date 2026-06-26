"""clear 命令 — 清空当前会话"""

name = "clear"
aliases = []
description = "清空当前会话"


def execute(agent, arg: str) -> tuple[bool, str]:
    agent.clear_session()
    return (True, "**🧹 当前会话已清空**")
