"""clear 命令 — 清空当前会话

直接操作 state.conversation.reset() + state.session.clear_session_file()，
不再经过 Agent.clear_session() 中转。
"""

from fp_core.core.state import State

name = "clear"
aliases: list[str] = []
description = "清空当前会话"


def execute(state: State, arg: str) -> tuple[bool, str]:
    # refresh=True：清空会话是显式重置动作，提示词随最新状态重建
    # （历史已清空，本来就要付一次前缀，不存在额外代价）
    state.rebuild_system_prompt(refresh=True)
    state.conversation.reset(state.conversation.system_prompt)
    state.session.clear_session_file()
    return (True, "**🧹 当前会话已清空**")
