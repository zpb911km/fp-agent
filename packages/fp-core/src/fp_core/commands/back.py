"""back 命令 — 回退到对话的某个历史时刻

用法:
  /back list           查看历史消息列表（仅非 system 消息，按 1-based 编号）
  /back <index>        回退到指定位置（删除后续消息）

全部逻辑通过公共 API 自组装，不依赖 core 层的业务策略。
"""

name = "back"
aliases: list[str] = []
description = "回退到对话的某个历史时刻。用法: /back list 查看列表, /back <N> 直接回退"


def _safe_preview(text: str, max_len: int = 80) -> str:
    """将消息内容截断并放入行内代码，防止残缺 MD 破坏全局渲染"""
    text = text.replace("`", "′")  # 反引号替换为类似字符，防止破坏代码块
    if len(text) > max_len:
        text = text[:max_len] + "..."
    return f"`{text}`"


async def execute(state, arg: str) -> tuple[bool, str]:
    parts = arg.strip().split()

    if not parts:
        return (True, "❌ 用法: `/back list` (查看列表) 或 `/back <N>` (直接回退)")

    cmd = parts[0]

    # ── 直接读 conversation 的非 system 消息（公共 API） ────
    history = state.conversation.get_non_system_messages()

    # ── /back list ─────────────────────────────────────────────
    if cmd == "list":
        if not history:
            return (True, "没有历史记录")

        roles_zh = {"user": "👤 用户", "assistant": "🤖 AI", "tool": "🔧 工具"}
        lines = [f"## 📜 对话历史（共 {len(history)} 条消息，使用 `/back <N>` 回退）"]
        for i, msg in enumerate(history):
            role = roles_zh.get(msg["role"], msg["role"])
            content = msg.get("content", "")
            if msg["role"] == "tool":
                preview = _safe_preview(content, max_len=60)
            else:
                preview = _safe_preview(content, max_len=120)
            lines.append(f"- **[{i + 1}] {role}**: {preview}")

        return (True, "\n".join(lines))

    # ── /back <index> ─────────────────────────────────────────
    try:
        index = int(cmd)
    except ValueError:
        return (True, f"❌ 无效参数：`{cmd}` — 请用 `/back list` 查看列表，`/back <N>` 回退")

    # 支持 `mode 参数` 但只接受 2（删除后续），mode=1 暂不支持
    if len(parts) >= 2:
        try:
            mode = int(parts[1])
            if mode != 2:
                return (True, "❌ mode=1（保留后续消息）暂不支持，请用 mode=2 或 `/fork`")
        except ValueError:
            return (True, f"❌ 无效参数：`{parts[1]}` 不是数字")

    # ── 索引验证 ─────────────────────────────────────────────
    if index < 1 or index > len(history):
        return (True, f"❌ 无效索引：{index}，有效范围 1~{len(history)}")

    # ── 执行回退（命令层自组装，纯公共 API） ────────────────
    # 1-based 序号直接当切片用：保留前 index 条非 system 消息
    kept = history[:index]
    state.conversation.set_messages(state.conversation.system_prompt, kept)
    # 持久化
    state.session.save_context(state.conversation.to_serializable())

    return (True, f"⏪ 已回退到第 {index} 条消息，后续消息已删除")
