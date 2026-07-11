"""clear 命令 — 清空当前会话

直接操作 state.conversation.reset() + state.session.clear_session_file()，
不再经过 Agent.clear_session() 中转。
"""

name = "clear"
aliases = []
description = "清空当前会话"


def execute(state, arg: str) -> tuple[bool, str]:
    state.rebuild_system_prompt()
    state.conversation.reset(state.conversation.system_prompt)
    state.session.clear_session_file()
    return (True, "**🧹 当前会话已清空**")
