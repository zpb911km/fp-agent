"""history 命令 — 查看当前对话历史"""

name = "history"
aliases = []
description = "查看当前对话历史"


def _safe_preview(text: str, max_len: int = 80) -> str:
    """将消息内容截断并放入行内代码，防止残缺 MD 破坏全局渲染"""
    text = text.replace("`", "′")  # 反引号替换为类似字符，防止破坏代码块
    if len(text) > max_len:
        text = text[:max_len] + "..."
    return f"`{text}`"


def execute(agent, arg: str) -> tuple[bool, str]:
    history_msgs = agent.history()

    if not history_msgs:
        return (True, "暂无对话历史")

    roles_zh = {"user": "👤 用户", "assistant": "🤖 AI", "tool": "🔧 工具"}
    lines = [f"## 📜 对话历史（共 {len(history_msgs)} 条）"]

    for i, msg in enumerate(history_msgs):
        role = roles_zh.get(msg["role"], msg["role"])
        content = msg.get("content", "")
        preview = _safe_preview(content, max_len=80) if msg["role"] == "tool" else _safe_preview(content, max_len=120)
        lines.append(f"- **[{i + 1}] {role}**: {preview}")

    output = "\n".join(lines)
    return (True, output)
