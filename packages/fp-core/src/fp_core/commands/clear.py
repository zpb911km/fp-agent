"""clear 命令 — 清空当前会话

直接操作 state.conversation.reset() + state.session.clear_session_file()，
不再经过 Agent.clear_session() 中转。
"""

from fp_core.core.state import State

name = "clear"
aliases: list[str] = []
description = "清空当前会话"


def execute(state: State, arg: str) -> tuple[bool, str]:
    state.rebuild_system_prompt()
    state.conversation.reset(state.conversation.system_prompt)
    state.session.clear_session_file()
    return (True, "**🧹 当前会话已清空**")
