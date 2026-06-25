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


async def execute(agent, arg: str) -> tuple[bool, str]:
    arg = arg.strip()

    # ── /resume delete list ────────────────────────────────────
    if arg == "delete list":
        sessions = agent.session.list_sessions()
        if not sessions:
            return (True, "暂无历史会话")

        current_sid = agent.session.session_id
        sorted_items = sorted(
            sessions.items(),
            key=lambda x: x[1].get("updated", ""),
            reverse=True,
        )

        deletable = [(sid, meta) for sid, meta in sorted_items if sid != current_sid]
        if not deletable:
            return (True, "没有可删除的会话（当前会话不可删除）")

        lines = [
            "## 🗑️ 可删除的会话",
            "使用 `/resume delete <sid>` 删除",
        ]
        for i, (sid, meta) in enumerate(deletable, 1):
            summary = meta.get("summary", "") or "(无摘要)"
            msg_count = meta.get("message_count", 0)
            created = meta.get("created", "?")[:16]
            lines.append(f"- **[{i}]** {summary} ({msg_count}条, {created}) [`{sid}`]")

        return (True, "\n".join(lines))

    # ── /resume delete <sid> ───────────────────────────────────
    if arg.startswith("delete "):
        sid = arg[7:].strip()
        if sid == agent.session.session_id:
            return (True, "❌ 不能删除当前正在使用的会话")
        if agent.delete_session(sid):
            return (True, f"🗑️ 已删除会话: `{sid}`")
        else:
            return (True, f"❌ 会话 `{sid}` 不存在或删除失败")

    # ── /resume delete (无参数) ─────────────────────────────────
    if arg == "delete":
        return (True, "❌ 用法: `/resume delete list` (查看可删除会话) 或 `/resume delete <sid>` (直接删除)")

    # ── /resume list ───────────────────────────────────────────
    if arg == "list":
        sessions = agent.session.list_sessions()
        if not sessions:
            return (True, "暂无历史会话")

        current_sid = agent.session.session_id
        sorted_items = sorted(
            sessions.items(),
            key=lambda x: x[1].get("updated", ""),
            reverse=True,
        )

        lines = [
            "## 📂 会话列表",
            "使用 `/resume <sid>` 切换，`/resume latest` 切换到最新",
        ]
        for i, (sid, meta) in enumerate(sorted_items, 1):
            summary = meta.get("summary", "") or "(无摘要)"
            msg_count = meta.get("message_count", 0)
            marker = " ⬅" if sid == current_sid else ""
            lines.append(f"- **[{i}]** {summary} ({msg_count}条, `{sid}`){marker}")

        return (True, "\n".join(lines))

    # ── /resume latest ─────────────────────────────────────────
    if arg == "latest":
        agent.resume_latest()
        return (True, f"📂 已切换到最新会话: `{agent.session.session_id}`")

    # ── /resume <无参数> ────────────────────────────────────────
    if not arg:
        return (True, "❌ 用法: `/resume list` (查看列表) | `/resume latest` | `/resume <sid>` (直接切换)")

    # ── /resume <sid> ──────────────────────────────────────────
    if agent.switch_session(arg):
        return (True, f"📂 已切换到会话: `{arg}`")
    else:
        return (True, f"❌ 会话 `{arg}` 不存在。使用 `/resume list` 查看可用会话")
