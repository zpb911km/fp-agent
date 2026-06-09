"""resume 命令 — 切换/删除历史会话

用法:
  /resume             交互式选择会话（编号输入）
  /resume latest      切换到最新会话
  /resume <sid>       切换到指定会话 ID
  /resume delete      交互式选择会话删除
  /resume delete <sid> 删除指定会话
"""


name = "resume"
aliases = []
description = "切换/删除历史会话。用法: /resume [latest|list|<sid>|delete [<sid>]]  无参则交互式选择"


def _display_width(s: str) -> int:
    """计算字符串的显示宽度（中文=2，英文/数字/符号=1）"""
    width = 0
    for c in s:
        if '\u4e00' <= c <= '\u9fff' or '\u3000' <= c <= '\u303f' or c in '（）':
            width += 2
        else:
            width += 1
    return width


def _pad_to_width(s: str, width: int) -> str:
    """用空格填充到指定显示宽度"""
    return s + ' ' * max(0, width - _display_width(s))


async def _interactive_select(agent) -> str | None:
    """
    异步编号选择列表 — 通过 agent.io 通道交互。
    返回选中的 sid，或 None（用户取消）。
    """
    sessions = agent.session.list_sessions()
    if not sessions:
        agent.io.info("暂无历史会话")
        return None

    current_sid = agent.session.session_id

    # 按 updated 降序排列
    sorted_items = sorted(
        sessions.items(),
        key=lambda x: x[1].get("updated", ""),
        reverse=True,
    )

    SUMMARY_WIDTH = 44  # 摘要区域显示宽度

    # 构建输出
    lines = ["📂 历史会话列表（输入编号切换，q 取消）:"]
    for i, (sid, meta) in enumerate(sorted_items, 1):
        raw_summary = meta.get("summary", "") or ""
        msg_count = meta.get("message_count", 0)
        created = meta.get("created", "?")[:16]
        marker = "  ⬅" if sid == current_sid else ""

        if not raw_summary:
            display_summary = "(无摘要)"
        else:
            display_summary = ""
            for ch in raw_summary:
                candidate = display_summary + ch
                if _display_width(candidate) > SUMMARY_WIDTH - 1:
                    display_summary += "…"
                    break
                display_summary = candidate

        line = f"  [{i}] {_pad_to_width(display_summary, SUMMARY_WIDTH)} ({msg_count:3d}条, {created}){marker}"
        lines.append(line)

    # 输出到 IO 通道
    agent.io.info(lines[0])
    for l in lines[1:]:
        agent.io.item(l)

    # 循环直到获得有效输入
    while True:
        raw = await agent.io.ask(f"请输入编号 (1-{len(sorted_items)}, 或 q 取消): ")

        if not raw:
            continue

        if raw.lower() in ("q", "quit", "exit", "cancel"):
            agent.io.info("已取消")
            return None

        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(sorted_items):
                return sorted_items[idx - 1][0]

        agent.io.error(f"无效输入，请输入 1-{len(sorted_items)} 或 q")


async def execute(agent, arg: str) -> tuple[bool, str]:
    arg = arg.strip()

    # /resume delete <sid> → 删除指定会话
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

    # /resume delete → 交互式选择删除
    if arg == "delete":
        sessions = agent.session.list_sessions()
        if not sessions:
            agent.io.info("暂无历史会话")
            return (True, "暂无历史会话")

        current_sid = agent.session.session_id
        sorted_items = sorted(
            sessions.items(),
            key=lambda x: x[1].get("updated", ""),
            reverse=True,
        )

        # 过滤掉当前会话
        deletable = [(sid, meta) for sid, meta in sorted_items if sid != current_sid]
        if not deletable:
            agent.io.info("没有可删除的会话（当前会话不可删除）")
            return (True, "无可删除会话")

        SUMMARY_WIDTH = 44
        lines = ["🗑️ 选择要删除的会话（输入编号，q 取消）:"]
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
                    if _display_width(candidate) > SUMMARY_WIDTH - 1:
                        display_summary += "…"
                        break
                    display_summary = candidate
            line = f"  [{i}] {_pad_to_width(display_summary, SUMMARY_WIDTH)} ({msg_count:3d}条, {created})"
            lines.append(line)

        agent.io.info(lines[0])
        for l in lines[1:]:
            agent.io.item(l)

        while True:
            raw = await agent.io.ask(f"请输入编号 (1-{len(deletable)}, 或 q 取消): ")
            if not raw:
                continue
            if raw.lower() in ("q", "quit", "exit", "cancel"):
                agent.io.info("已取消")
                return (True, "已取消")
            if raw.isdigit():
                idx = int(raw)
                if 1 <= idx <= len(deletable):
                    sid = deletable[idx - 1][0]
                    # 确认
                    confirm = await agent.io.ask(f"确定删除会话 {sid[:12]}...? (y/N): ")
                    if confirm and confirm.lower() == "y":
                        if agent.delete_session(sid):
                            msg = f"🗑️ 已删除会话: {sid[:16]}..."
                            agent.io.info(msg)
                            return (True, msg)
                        else:
                            msg = "❌ 删除失败"
                            agent.io.error(msg)
                            return (True, msg)
                    else:
                        agent.io.info("已取消")
                        return (True, "已取消")
            agent.io.error(f"无效输入，请输入 1-{len(deletable)} 或 q")

    # /resume latest
    if arg == "latest":
        agent.resume_latest()
        msg = f"📂 已切换到最新会话: {agent.session.session_id}"
        agent.io.info(msg)
        return (True, msg)

    # /resume 或 /resume list → 交互式选择
    if not arg or arg == "list":
        sid = await _interactive_select(agent)
        if sid is None:
            return (True, "已取消")
        if agent.switch_session(sid):
            msg = "📂 已切换到会话"
            agent.io.info(msg)
            return (True, msg)
        else:
            msg = "❌ 会话不存在"
            agent.io.error(msg)
            return (True, msg)

    # /resume <sid>
    if agent.switch_session(arg):
        msg = f"📂 已切换到会话: {arg}"
        agent.io.info(msg)
        return (True, msg)
    else:
        msg = f"❌ 会话 {arg} 不存在"
        agent.io.error(msg)
        return (True, msg)
