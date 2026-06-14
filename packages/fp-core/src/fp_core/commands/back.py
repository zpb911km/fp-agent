"""back 命令 — 回退到对话的某个历史时刻

用法:
  /back list           查看历史消息列表（仅非 system 消息，按 1-based 编号）
  /back <index>        回退到指定位置（删除后续消息）
  /back <index> 2      同上（删除后续消息）
"""

name = "back"
aliases = []
description = "回退到对话的某个历史时刻。用法: /back list 查看列表, /back <N> 直接回退"


async def execute(agent, arg: str) -> tuple[bool, str]:
    parts = arg.strip().split()

    if not parts:
        msg = "❌ 用法: /back list (查看列表) 或 /back <N> (直接回退)"
        agent.io.error(msg)
        return (True, msg)

    cmd = parts[0]

    # ── /back list ─────────────────────────────────────────────
    if cmd == "list":
        history_msgs = agent.get_history_for_display()
        if not history_msgs:
            msg = "没有历史记录"
            agent.io.info(msg)
            return (True, msg)

        roles_zh = {"user": "👤 用户", "assistant": "🤖 AI", "tool": "🔧 工具"}
        lines = [f"📜 对话历史（共 {len(history_msgs)} 条消息，使用 /back <N> 回退）:"]
        for i, msg in enumerate(history_msgs):
            role = roles_zh.get(msg["role"], msg["role"])
            content = msg.get("content", "")
            if msg["role"] == "tool":
                content = content[:60] + "..." if len(content) > 60 else content
            else:
                content = content[:120] + "..." if len(content) > 120 else content
            content = content.replace("\n", " ")
            lines.append(f"  [{i + 1:3d}] {role}: {content}")

        agent.io.info(lines[0])
        for line in lines[1:]:
            agent.io.item(line)

        return (True, "\n".join(lines))

    # ── /back <index> [2] ──────────────────────────────────────
    try:
        index = int(cmd)
    except ValueError:
        msg = f"❌ 无效参数：'{cmd}' — 请用 /back list 查看列表，/back <N> 回退"
        agent.io.error(msg)
        return (True, msg)

    mode = None
    if len(parts) >= 2:
        try:
            mode = int(parts[1])
            if mode != 2:
                msg = "❌ mode=1（保留后续消息）暂不支持，请用 mode=2 或 /fork"
                agent.io.error(msg)
                return (True, msg)
        except ValueError:
            msg = f"❌ 无效参数：'{parts[1]}' 不是数字"
            agent.io.error(msg)
            return (True, msg)

    result = await agent.back(target_idx=index, mode=mode)
    agent.io.info(result)
    return (True, result)
