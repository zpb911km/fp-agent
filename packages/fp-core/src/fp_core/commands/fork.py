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

    # 记录分叉点：fork 是**物理复制**消息，父子共享前缀。关键词若照算，
    # 同组会话会得到几乎相同的词（实测组内 Jaccard 0.372 vs 非 fork 0.080）。
    # 写下 fork_from 后，关键词只统计分叉之后的增量内容，且免去 LCP 扫描。
    state.session.update_meta(fork_from={"sid": old_sid, "msg_count": len(old_messages)})

    return (True, f"🍴 已 fork：从 {old_sid} → {new_sid}")
