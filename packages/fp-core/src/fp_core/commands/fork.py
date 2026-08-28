"""fork 命令 — 基于当前上下文新建会话

直接操作 state.conversation + state.session，
不再经过 Agent.fork() 中转。
"""

from fp_core.core.state import State

name = "fork"
aliases: list[str] = []
description = "基于当前上下文新建会话"


def execute(state: State, arg: str) -> tuple[bool, str]:
    old_messages = state.conversation.get_non_system_messages()

    if not old_messages:
        return (True, "当前会话没有消息，无法 `fork`")

    # 保存当前会话（上下文 + 摘要）
    old_sid = state.session_id
    state.session.save_and_summarize(state.conversation.to_serializable(), old_sid)
    new_sid = state.session.create_session()

    # 重建上下文：用当前 system prompt + 旧消息
    system_prompt = state.conversation.system_prompt
    state.conversation.set_messages(system_prompt, old_messages)
    state.session.save_context(state.conversation.to_serializable())

    return (True, f"🍴 已 fork：从 {old_sid} → {new_sid}")
