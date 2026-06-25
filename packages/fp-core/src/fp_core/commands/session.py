"""session 命令 — 显示当前会话信息"""

name = "session"
aliases = []
description = "显示当前会话信息"


def execute(agent, arg: str) -> tuple[bool, str]:
    return (True, f"**📂 当前会话**: `{agent.session.session_id}`")
