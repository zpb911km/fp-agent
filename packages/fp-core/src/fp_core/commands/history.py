"""history 命令 — 查看当前对话历史

直接读 state.conversation，不再经过 Agent.history() 中转。
"""

name = "history"
aliases: list[str] = []
description = "查看当前对话历史"


def _safe_preview(text: str, max_len: int = 80) -> str:
    """将消息内容截断并放入行内代码，防止残缺 MD 破坏全局渲染"""
    text = text.replace("`", "′")  # 反引号替换为类似字符
    if len(text) > max_len:
        text = text[:max_len] + "..."
    return f"`{text}`"


def execute(state, arg: str) -> tuple[bool, str]:
    history = state.conversation.get_history_for_display()

    if not history:
        return (True, "暂无对话历史")

    roles_zh = {"user": "👤 用户", "assistant": "🤖 AI", "tool": "🔧 工具"}
    lines = [f"## 📜 对话历史（共 {len(history)} 条）"]

    for i, msg in enumerate(history):
        role = roles_zh.get(msg["role"], msg["role"])
        content = msg.get("content", "")
        preview = _safe_preview(content, max_len=80) if msg["role"] == "tool" else _safe_preview(content, max_len=120)
        lines.append(f"- **[{i + 1}] {role}**: {preview}")

    return (True, "\n".join(lines))
