"""shortcircuit 命令 — 短路已完成的连通块

将已完成的交互（连通块）压缩为 user + assistant 消息对，
移除中间的工具调用细节。

用法:
  /sc                    短路最近 1 个可压缩态的连通块
  /sc N                  短路最近 N 个可压缩态的连通块
  /sc list               显示所有连通块概览
  /sc #N                 短路编号为 N 的连通块
  /sc #M-#N              短路编号 M 到 N 的连通块

可选修饰（跟在最后）:
  -c                     裁剪模式（crop）：只移除 tool 中间消息，不调 LLM
  -r                     提炼模式（regenerate）：调 LLM 重新生成精简回复（默认）
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
        agent.io.error(msg)
        return (True, msg)

    # ── /sc list ─────────────────────────────────────────
    if action == "list":
        components = agent.scan_components()
        if not components:
            msg = "没有已完成的连通块"
            agent.io.info(msg)
            return (True, msg)
        display_text = _format_components_display(components)
        for line in display_text.split("\n"):
            if line.startswith("📦"):
                agent.io.info(line)
            elif line.strip():
                agent.io.item(line)
        return (True, display_text)

    # ── 执行短路 ─────────────────────────────────────────
    if action in ("default", "count", "index", "range"):
        agent.io.info("🔄 正在短路...")
    else:
        msg = "未知错误"
        agent.io.error(msg)
        return (True, msg)

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
        agent.io.info(f" ✅\n📦 {detail}")
        return (True, f"✅ {detail}")
    else:
        agent.io.error(f" ❌\n{result['msg']}")
        return (True, result["msg"])


def _format_components_display(components: list[dict]) -> str:
    """格式化连通块列表用于 /sc list 展示"""
    if not components:
        return "没有已完成的连通块"

    lines = [f"📦 连通块列表（共 {len(components)} 个，~ = 已充分压缩）:"]
    for comp in components:
        flag = " ~" if not comp["compressible"] else "  "
        user_text = comp["user_preview"][:80].replace("\n", " ")
        ai_text = comp["assistant_preview"][:80].replace("\n", " ")
        msg_count = comp["message_count"]
        lines.append(f"  #{comp['idx']:2d}{flag} [用户] {user_text}")
        lines.append(f"         [AI]   {ai_text}  ({msg_count}条)")
        lines.append("")
    return "\n".join(lines)
