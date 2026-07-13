"""new 命令 — 新建一个空白会话

逻辑复用 fork + clear：

  fork 阶段：
    1. 保存当前会话上下文至文件
    2. 记录旧会话摘要（最后一条消息）
    3. 分配新会话 ID

  clear 阶段：
    4. 重建 system prompt
    5. 重置 conversation 为空
    6. 清空 session 文件（只保留 meta header）

结果：旧会话被保存归档，当前会话切换为全新的空白会话。
"""

name = "new"
aliases = ["fresh"]
description = "新建一个空白会话（保存当前会话后创建全新会话）"


def execute(state, arg: str) -> tuple[bool, str]:
    # ── fork 阶段：保存旧会话 ──────────────────────────────────
    old_messages = state.conversation.get_non_system_messages()
    old_sid = state.session_id

    # 无论是否有消息都保存，避免丢历史
    state.session.save_context(state.conversation.to_serializable())

    # 提取摘要（最后一条消息的前 50 字）
    last_msg_content = old_messages[-1].get("content", "")[:50] if old_messages else ""

    # 更新旧会话的 meta 摘要
    state.session.update_meta(old_sid, summary=last_msg_content)

    # 创建新会话（lazy file，此时只是分配 ID + 切换 session_id）
    new_sid = state.session.create_session()

    # ── clear 阶段：重置为新空白会话 ──────────────────────────
    state.rebuild_system_prompt()
    state.conversation.reset(state.conversation.system_prompt)
    state.session.clear_session_file()

    return (True, f"🆕 已创建新会话：`{new_sid}`（旧会话 `{old_sid}` 已保存）")
