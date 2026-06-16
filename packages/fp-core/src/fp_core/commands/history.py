"""history 命令 — 查看当前对话历史"""

from fp_core import display

name = "history"
aliases = []
description = "查看当前对话历史"


def execute(agent, arg: str) -> tuple[bool, str]:
    history_msgs = agent.history()

    if not history_msgs:
        return (True, "暂无对话历史")

    roles_zh = {"user": "👤 用户", "assistant": "🤖 AI", "tool": "🔧 工具"}
    lines = [f"📜 对话历史（共 {len(history_msgs)} 条）:"]

    for i, msg in enumerate(history_msgs):
        role = roles_zh.get(msg["role"], msg["role"])
        content = msg.get("content", "")
        if msg["role"] == "tool":
            content = content[:80] + "..." if len(content) > 80 else content
        else:
            content = content[:120] + "..." if len(content) > 120 else content
        content = content.replace("\n", " ")
        lines.append(f"  [{i + 1:3d}] {role}: {content}")

    output = "\n".join(lines)
    display.info(f"\n{output}")
    return (True, output)
