"""shortcircuit 命令 — 短路连通块

将交互（连通块）压缩为 user + assistant 消息对，
移除中间的工具调用细节。

直接操作 state.conversation.(scan_components|shortcircuit) + state.llm.summarize，
不再经过 Agent 中转。

用法:
  /sc                    短路最近 1 个可压缩态的连通块
  /sc N                  短路最近 N 个可压缩态的连通块
  /sc list               显示所有连通块概览
  /sc #N                 短路编号为 N 的连通块
  /sc #M-#N              短路编号 M 到 N 的连通块

可选修饰（跟在最后）:
  -c                     裁剪模式（crop）：只移除 tool 中间消息，不调 LLM（默认）
  -r                     提炼模式（regenerate）：调 LLM 重新生成精简回复

状态标记（/sc list）:
  ~ = 已充分压缩（无可压缩空间）
  * = 未完成（中断块 / 待回复）
"""

from collections.abc import Callable

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
        return ("default", 1, "crop")

    # 从后往前提取修饰符
    clean_parts: list[str] = []
    mode = "crop"
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

    if cmd == "list":
        return ("list", None, mode)

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

    try:
        n = int(cmd)
        if n < 1:
            return ("error", f"数量必须大于 0，收到 {n}", mode)
        return ("count", n, mode)
    except ValueError:
        return ("error", f"无效参数: '{cmd}'", mode)


def _build_regenerate_refiner(state) -> Callable:
    """构建提炼回调 — 调用 LLM 精炼 assistant 回复"""

    async def refiner(user_text: str, assistant_text: str, context_text: str) -> tuple[str, str]:
        is_interrupted = assistant_text == "_INTERRUPTED_"
        prompt = (
            "以下是一段 AI 与用户之间的完整对话，包含工具调用过程。\n\n"
            "任务：将这段对话压缩为第一人称操作日志，保留 AI 做了什么、发现了什么、决策了什么。\n\n"
            "要求：\n"
            "1. 【第一人称叙述】以「我」的视角，按时间顺序描述："
            "我做了什么操作 → 发现了什么 → 得出了什么结论 → 做了什么决策。\n"
            "2. 【保留关键产出】工具调用的参数/命令细节可以丢弃，但工具的发现结果必须保留："
            "查到了什么数据、找到了什么文件、确认了什么状态、修改了什么代码。\n"
            "3. 【信息密度】压缩到原回复的 1/3~1/2 长度。"
            "重点保留：文件路径、函数名、变量名、错误信息、数值、决策理由。\n"
            "4. 【过程精炼，结果展开】过程描述控制在 1~2 句话概括做了什么、为什么做；"
            "最终结果（创建的/修改了什么、测试结论、状态变化、发现的数据）展开保留，不得过度压缩。\n"
            "5. 【保留末尾总结】如果最后一条输出是总结性陈述"
            "（如「搞定」、「改完了」、「测试通过」、具体数值等），"
            "则原样输出最后一次回复的完整内容。\n"
            "6. 只输出压缩后的内容，不要任何前缀或格式说明。\n"
            "7. 如果对话被中断，在末尾加上「（对话被中断）」"
        )
        try:
            result = await state.llm.summarize(
                context_text,
                instruction=prompt,
                system_prompt=(
                    "你是一个第一人称操作日志记录助手。"
                    "将工具调用过程压缩为连贯的"
                    "「我做了什么→发现了什么→决策了什么」日志，"
                    "丢弃工具调用细节，保留发现和决策。"
                ),
                max_tokens=8192,
            )
            refined = (result or "").strip()
            if not refined:
                refined = "被用户中断" if is_interrupted else assistant_text
        except Exception:
            refined = "被用户中断" if is_interrupted else assistant_text
        return (user_text, refined)

    return refiner


async def execute(state, arg: str) -> tuple[bool, str]:
    action, value, mode = _parse_args(arg)

    if action == "error":
        return (True, f"❌ {value}")

    # ── /sc list ─────────────────────────────────────────
    if action == "list":
        components = state.conversation.scan_components()
        if not components:
            return (True, "没有已完成的连通块")
        return (True, _format_components_display(components))

    # ── 执行短路 ─────────────────────────────────────────

    components = state.conversation.scan_components()
    if not components:
        return (True, "没有已完成的连通块需要短路")

    # 确定要短路的原始索引
    if action == "default":
        native = [c for c in reversed(components) if c["compressible"]]
        selected = native[:1]
        target_raw = [(c["user_idx"], c["terminal_idx"]) for c in selected]
    elif action == "count":
        assert isinstance(value, int)
        native = [c for c in reversed(components) if c["compressible"]]
        selected = native[:value]
        target_raw = [(c["user_idx"], c["terminal_idx"]) for c in selected]
    elif action == "index":
        target_raw = []
        for comp in components:
            if comp["idx"] == value:
                target_raw.append((comp["user_idx"], comp["terminal_idx"]))
                break
    elif action == "range":
        assert isinstance(value, tuple) and len(value) == 2
        start, end = value
        selected = [c for c in components if start <= c["idx"] <= end]
        if not selected:
            return (True, f"未找到编号 {start}~{end} 的连通块")
        min_user = selected[0]["user_idx"]
        max_terminal = selected[-1]["terminal_idx"]
        target_raw = [(min_user, max_terminal)]
    else:
        return (True, "未知操作")

    if not target_raw:
        return (True, "没有可短路的连通块，或指定的连通块编号不存在")

    refiner = None if mode == "crop" else _build_regenerate_refiner(state)
    success, msg, saved = await state.conversation.shortcircuit(refiner, target_raw, mode)

    if success:
        state.session.save_context(state.conversation.to_serializable())
        return (True, f"✅ 已处理 {len(target_raw)} 个连通块，节省 {saved} 条消息")
    else:
        return (True, msg)


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
