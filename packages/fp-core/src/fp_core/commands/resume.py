"""resume 命令 — 切换/删除历史会话（非交互式）

用法:
  /resume list              列出所有会话
  /resume latest            切换到最新会话
  /resume <sid>             切换到指定会话
  /resume delete list       列出可删除的会话
  /resume delete <sid>      删除指定会话
"""

name = "resume"
aliases = []
description = "切换/删除历史会话。用法: /resume list, /resume latest, /resume <sid>, /resume delete <sid>"


def _display_width(s: str) -> int:
    """计算字符串的显示宽度（中文=2，英文/数字/符号=1）"""
    width = 0
    for c in s:
        if "\u4e00" <= c <= "\u9fff" or "\u3000" <= c <= "\u303f" or c in "（）":
            width += 2
        else:
            width += 1
    return width


def _pad_to_width(s: str, width: int) -> str:
    """用空格填充到指定显示宽度"""
    return s + " " * max(0, width - _display_width(s))


async def execute(agent, arg: str) -> tuple[bool, str]:
    arg = arg.strip()

    # ── /resume delete list ────────────────────────────────────
    if arg == "delete list":
        sessions = agent.session.list_sessions()
        if not sessions:
            msg = "暂无历史会话"
            agent.io.info(msg)
            return (True, msg)

        current_sid = agent.session.session_id
        sorted_items = sorted(
            sessions.items(),
            key=lambda x: x[1].get("updated", ""),
            reverse=True,
        )

        deletable = [(sid, meta) for sid, meta in sorted_items if sid != current_sid]
        if not deletable:
            msg = "没有可删除的会话（当前会话不可删除）"
            agent.io.info(msg)
            return (True, msg)

        summary_width = 44
        lines = ["🗑️ 可删除的会话（使用 /resume delete <sid> 删除）:"]
        for i, (sid, meta) in enumerate(deletable, 1):
            raw_summary = meta.get("summary", "") or ""
            msg_count = meta.get("message_count", 0)
            created = meta.get("created", "?")[:16]
            if not raw_summary:
                display_summary = "(无摘要)"
            else:
                display_summary = ""
                for ch in raw_summary:
                    candidate = display_summary + ch
                    if _display_width(candidate) > summary_width - 1:
                        display_summary += "…"
                        break
                    display_summary = candidate
            line = f"  [{i}] {_pad_to_width(display_summary, summary_width)} ({msg_count:3d}条, {created})  [{sid}]"
            lines.append(line)

        agent.io.info(lines[0])
        for line in lines[1:]:
            agent.io.item(line)

        return (True, "\n".join(lines))

    # ── /resume delete <sid> ───────────────────────────────────
    if arg.startswith("delete "):
        sid = arg[7:].strip()
        if sid == agent.session.session_id:
            msg = "❌ 不能删除当前正在使用的会话"
            agent.io.error(msg)
            return (True, msg)
        if agent.delete_session(sid):
            msg = f"🗑️ 已删除会话: {sid}"
            agent.io.info(msg)
            return (True, msg)
        else:
            msg = f"❌ 会话 {sid} 不存在或删除失败"
            agent.io.error(msg)
            return (True, msg)

    # ── /resume delete (无参数) ─────────────────────────────────
    if arg == "delete":
        msg = "❌ 用法: /resume delete list (查看可删除会话) 或 /resume delete <sid> (直接删除)"
        agent.io.error(msg)
        return (True, msg)

    # ── /resume list ───────────────────────────────────────────
    if arg == "list":
        sessions = agent.session.list_sessions()
        if not sessions:
            msg = "暂无历史会话"
            agent.io.info(msg)
            return (True, msg)

        current_sid = agent.session.session_id
        sorted_items = sorted(
            sessions.items(),
            key=lambda x: x[1].get("updated", ""),
            reverse=True,
        )

        summary_width = 44
        lines = ["📂 会话列表（使用 /resume <sid> 切换，/resume latest 切换到最新）:"]
        for i, (sid, meta) in enumerate(sorted_items, 1):
            raw_summary = meta.get("summary", "") or ""
            msg_count = meta.get("message_count", 0)
            marker = "  ⬅" if sid == current_sid else ""

            if not raw_summary:
                display_summary = "(无摘要)"
            else:
                display_summary = ""
                for ch in raw_summary:
                    candidate = display_summary + ch
                    if _display_width(candidate) > summary_width - 1:
                        display_summary += "…"
                        break
                    display_summary = candidate

            line = f"  [{i}] {_pad_to_width(display_summary, summary_width)} ({msg_count:3d}条, {sid}){marker}"
            lines.append(line)

        agent.io.info(lines[0])
        for line in lines[1:]:
            agent.io.item(line)

        return (True, "\n".join(lines))

    # ── /resume latest ─────────────────────────────────────────
    if arg == "latest":
        agent.resume_latest()
        msg = f"📂 已切换到最新会话: {agent.session.session_id}"
        agent.io.info(msg)
        return (True, msg)

    # ── /resume <无参数> ────────────────────────────────────────
    if not arg:
        msg = "❌ 用法: /resume list (查看列表) | /resume latest | /resume <sid> (直接切换)"
        agent.io.error(msg)
        return (True, msg)

    # ── /resume <sid> ──────────────────────────────────────────
    if agent.switch_session(arg):
        msg = f"📂 已切换到会话: {arg}"
        agent.io.info(msg)
        return (True, msg)
    else:
        msg = f"❌ 会话 {arg} 不存在。使用 /resume list 查看可用会话"
        agent.io.error(msg)
        return (True, msg)
