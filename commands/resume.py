"""resume 命令 — 切换历史会话

用法:
  /resume          交互式选择会话（编号输入）
  /resume latest   切换到最新会话
  /resume <sid>    切换到指定会话 ID
"""

import sys

import display


name = "resume"
aliases = []
description = "切换历史会话。用法: /resume [latest|list|<sid>]  无参则交互式选择"


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


def _interactive_select(agent) -> str | None:
    """
    编号选择列表 — 列出所有会话让用户输入编号切换。
    返回选中的 sid，或 None（用户取消）。
    非 TTY 环境下降级为纯文本列表。
    """
    if not sys.stdin.isatty():
        display.info(agent.list_sessions())
        return None

    sessions = agent.session.list_sessions()
    if not sessions:
        display.info("暂无历史会话")
        return None

    current_sid = agent.session.session_id

    # 按 updated 降序排列
    sorted_items = sorted(
        sessions.items(),
        key=lambda x: x[1].get("updated", ""),
        reverse=True,
    )

    SUMMARY_WIDTH = 44  # 摘要区域显示宽度

    # 打印列表
    print()
    display.info("📂 历史会话列表（输入编号切换，q 取消）:")
    for i, (sid, meta) in enumerate(sorted_items, 1):
        raw_summary = meta.get("summary", "") or ""
        msg_count = meta.get("message_count", 0)
        created = meta.get("created", "?")[:16]
        marker = "  ⬅" if sid == current_sid else ""

        # 处理空摘要
        if not raw_summary:
            display_summary = "(无摘要)"
        else:
            # 逐字截断，保证显示宽度不超过 SUMMARY_WIDTH
            display_summary = ""
            for ch in raw_summary:
                candidate = display_summary + ch
                if _display_width(candidate) > SUMMARY_WIDTH - 1:
                    display_summary += "…"
                    break
                display_summary = candidate

        line = f"  [{i}] {_pad_to_width(display_summary, SUMMARY_WIDTH)} ({msg_count:3d}条, {created}){marker}"
        print(line)

    # 输入选择
    while True:
        try:
            raw = input(f"\n请输入编号 (1-{len(sorted_items)}, 或 q 取消): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            display.info("已取消")
            return None

        if not raw:
            continue

        if raw.lower() in ("q", "quit", "exit", "cancel"):
            display.info("已取消")
            return None

        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(sorted_items):
                return sorted_items[idx - 1][0]

        print(f"  ⚠️  无效输入，请输入 1-{len(sorted_items)} 或 q")


def execute(agent, arg: str) -> bool:
    arg = arg.strip()

    # /resume latest
    if arg == "latest":
        sid = agent.session.resume_latest()
        agent._context = agent._build_context()
        display.info(f"📂 已切换到最新会话: {agent.session.session_id}")
        return True

    # /resume 或 /resume list → 交互式选择
    if not arg or arg == "list":
        sid = _interactive_select(agent)
        if sid is None:
            return True
        if agent.switch_session(sid):
            display.info(f"📂 已切换到会话")
        else:
            display.info("❌ 会话不存在")
        return True

    # /resume <sid>
    if agent.switch_session(arg):
        display.info(f"📂 已切换到会话: {arg}")
    else:
        display.info(f"❌ 会话 {arg} 不存在")
    return True
