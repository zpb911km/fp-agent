"""fork 命令 — 基于当前上下文新建会话

直接操作 state.conversation + state.session，
不再经过 Agent.fork() 中转。
"""

name = "fork"
aliases = []
description = "基于当前上下文新建会话"


def execute(state, arg: str) -> tuple[bool, str]:
    old_messages = state.conversation.get_non_system_messages()

    if not old_messages:
        return (True, "当前会话没有消息，无法 `fork`")

    # 保存当前会话
    state.session.save_context(state.conversation.messages)

    last_msg_content = old_messages[-1].get("content", "")[:50] if old_messages else ""
    old_sid = state.session_id
    new_sid = state.session.create_session()

    # 重建上下文：用当前 system prompt + 旧消息
    system_prompt = state.conversation.system_prompt
    state.conversation.set_messages(system_prompt, old_messages)
    state.session.save_context(state.conversation.messages)

    # 更新旧会话摘要
    state.session.update_meta(old_sid, summary=last_msg_content)

    return (True, f"🍴 已 fork：从 {old_sid} → {new_sid}")
