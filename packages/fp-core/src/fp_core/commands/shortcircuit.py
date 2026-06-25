"""shortcircuit 命令 — 短路连通块

将交互（连通块）压缩为 user + assistant 消息对，
移除中间的工具调用细节。

连通块按 user 消息分隔切割，覆盖所有消息（包括中断块）。
中断块压缩后标记为"被用户中断"或由 LLM 提炼保留有效信息。

用法:
  /sc                    短路最近 1 个可压缩态的连通块
  /sc N                  短路最近 N 个可压缩态的连通块
  /sc list               显示所有连通块概览
  /sc #N                 短路编号为 N 的连通块
  /sc #M-#N              短路编号 M 到 N 的连通块

可选修饰（跟在最后）:
  -c                     裁剪模式（crop）：只移除 tool 中间消息，不调 LLM
  -r                     提炼模式（regenerate）：调 LLM 重新生成精简回复（默认）

状态标记（/sc list）:
  ~ = 已充分压缩（无可压缩空间）
  * = 未完成（中断块 / 待回复）
"""

name = "sc"
description = "短路(shortcircuit)已完成的连通块。用法: /sc list 查看, /sc 或 /sc N 短路最近的, /sc #N 短路指定编号的"


def _parse_args(arg: str) -> tuple[str, object, str]:
    """解析短路命令参数

    Returns:
        (action, value, mode)
        action: "list" | "default" | "count" | "index" | "range" | "error"
        value:  int | tuple(int,int) | str(错误信息)
        mode:   "regenerate" | "crop"
    """
    parts = arg.strip().split()
    if not parts:
        return ("default", 1, "regenerate")

    # 从后往前提取修饰符
    clean_parts: list[str] = []
    mode = "regenerate"
    for p in parts:
        if p == "-c":
            mode = "crop"
        elif p == "-r":
            mode = "regenerate"
        else:
            clean_parts.append(p)

    if not clean_parts:
        return ("default", 1, mode)

    cmd = clean_parts[0]

    # /sc list
    if cmd == "list":
        return ("list", None, mode)

    # /sc #N 或 /sc #M-#N
    if cmd.startswith("#"):
        if "-" in cmd:
            parts_range = cmd.split("-")
            try:
                start = int(parts_range[0].lstrip("#"))
                end = int(parts_range[1].lstrip("#"))
            except ValueError:
                return ("error", f"无效范围: '{cmd}'", mode)
            if start > end:
                return ("error", f"起始编号 {start} 大于终止编号 {end}", mode)
            return ("range", (start, end), mode)
        else:
            try:
                return ("index", int(cmd.lstrip("#")), mode)
            except ValueError:
                return ("error", f"无效编号: '{cmd}'", mode)

    # /sc N
    try:
        n = int(cmd)
        if n < 1:
            return ("error", f"数量必须大于 0，收到 {n}", mode)
        return ("count", n, mode)
    except ValueError:
        return ("error", f"无效参数: '{cmd}'", mode)


async def execute(agent, arg: str) -> tuple[bool, str]:
    action, value, mode = _parse_args(arg)

    # ── 错误 ─────────────────────────────────────────────
    if action == "error":
        msg = f"❌ {value}"
        return (True, msg)

    # ── /sc list ─────────────────────────────────────────
    if action == "list":
        components = agent.scan_components()
        if not components:
            return (True, "没有已完成的连通块")
        display_text = _format_components_display(components)
        return (True, display_text)

    # ── 执行短路 ─────────────────────────────────────────
    result: dict = {"ok": False, "msg": "", "saved": 0, "count": 0}
    if action == "default":
        result = await agent.shortcircuit_context(count=1, mode=mode)
    elif action == "count":
        result = await agent.shortcircuit_context(count=value, mode=mode)
    elif action == "index":
        result = await agent.shortcircuit_context(indices=[value], mode=mode)
    elif action == "range":
        assert isinstance(value, tuple) and len(value) == 2
        start, end = value
        # 合并范围内的所有连通块为单一范围
        components = agent.scan_components()
        selected = [c for c in components if start <= c["idx"] <= end]
        if not selected:
            result = {"ok": False, "msg": f"未找到编号 {start}~{end} 的连通块", "saved": 0, "count": 0}
        else:
            min_user = selected[0]["user_idx"]
            max_terminal = selected[-1]["terminal_idx"]
            result = await agent.shortcircuit_context(raw_indices=[(min_user, max_terminal)], mode=mode)

    if result["ok"]:
        detail = f"已处理 {result['count']} 个连通块，节省 {result['saved']} 条消息"
        return (True, f"✅ {detail}")
    else:
        return (True, result["msg"])


def _format_components_display(components: list[dict]) -> str:
    """格式化连通块列表用于 /sc list 展示"""
    if not components:
        return "没有已完成的连通块"

    lines = [
        f"## 📦 连通块列表（共 {len(components)} 个）",
        "`~` = 已充分压缩，`*` = 未完成",
    ]
    for comp in components:
        msg_count = comp["message_count"]
        complete = comp["complete"]

        # 决定状态标记
        if msg_count == 1:
            flag = "`*`"
            ai_preview = "（待回复）"
        elif not complete:
            flag = "`*`"
            ai_preview = comp["assistant_preview"][:80].replace("\n", " ")
        elif msg_count == 2 and not comp["compressible"]:
            flag = "`~`"
            ai_preview = comp["assistant_preview"][:80].replace("\n", " ")
        else:
            flag = ""
            ai_preview = comp["assistant_preview"][:80].replace("\n", " ")

        user_text = comp["user_preview"][:80].replace("\n", " ")
        flag_part = f" {flag}" if flag else ""
        lines.append(f"- **#{comp['idx']}**{flag_part} **用户**: {user_text}")
        lines.append(f"  **AI**: {ai_preview} ({msg_count}条)")

    return "\n".join(lines)
