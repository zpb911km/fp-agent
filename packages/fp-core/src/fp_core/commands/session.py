"""session 命令 — 显示当前会话信息

读 state.session_id，不再经过 Agent 中转。
"""

name = "session"
aliases: list[str] = []
description = "显示当前会话信息"


def execute(state, arg: str) -> tuple[bool, str]:
    return (True, f"**📂 当前会话**: `{state.session_id}`")
