"""shortcircuit 核心 — 连通块扫描 / 退化（degenerate）/ 合并（crop/regenerate）

shortcircuit 插件包的核心实现：连通块检测、退化、合并、命令与工具的执行逻辑。
命令 `/sc` 与工具 `shortcircuit` 共用本模块的纯函数，不依赖 core 层业务策略。

命令用法（/sc）:
  /sc                    短路最近 1 个可压缩态的连通块
  /sc N                  短路最近 N 个可压缩态的连通块
  /sc list               显示所有连通块概览
  /sc #N                 短路编号为 N 的连通块
  /sc #M-#N              短路编号 M 到 N 的连通块

可选修饰（跟在最后）:
  -c                     裁剪模式（crop）：只移除 tool 中间消息，不调 LLM（默认）
  -r                     提炼模式（regenerate）：调 LLM 重新生成精简回复
  -d                     退化模式（degenerate）：删除工具调用/工具返回消息；user 类消息
                        （人类话语与 ⁂[kind] 注入）一律原地保留；每个调用点先吸收
                        其前紧邻的连续纯文本、再剥壳转正 —— 工具调用链退化为纯文本
                        assistant 消息链（保留 AI 文本记录）

硬性策略（仅 shortcircuit 工具，命令层 /sc 不适用）:
  - 当前块（最后一个连通块）只能使用 -d（degenerate），其余模式被强制覆盖
  - 其他块默认 crop：未显式指定 mode 时按 crop 处理
  - 序号/范围/批量指定涉及当前块时：其他块按指定行为，当前块强制 -d
  - 命令层 /sc 保持自由：按显式 -c/-r/-d 处理任意块（含当前块），无硬性约束

状态标记（/sc list）:
  D = 可退化（块内存在工具调用噪音）
  C = 可压缩（crop 有收益，无工具噪音）
  ~ = 已充分压缩（无可压缩空间）
  * = 未完成（中断块 / 待回复）
"""

from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from fp_core.core.jobs import is_injected

Message = dict[str, Any]


class Component(TypedDict):
    """连通块记录（见 scan_components 的文档注释）"""

    idx: int
    user_idx: int
    terminal_idx: int
    message_count: int
    user_preview: str
    assistant_preview: str
    compressible: bool
    complete: bool
    degenerable: bool


Refiner = Callable[[str, str, str], Awaitable[tuple[str, str]]]

# 已转正调用点标记：下划线私有键，get_messages_for_llm 发往 API 前整键剥除。
# 作用是给「吸收前沿连续纯文本」划定边界——已转正的调用点不是纯文本，
# 否则链式调用（调用点→文本→调用点）会层层吞并，塌成一条。
SC_CALLSITE = "_sc_callsite"


# ═══════════════════════════════════════════════════════
# 核心纯函数（plugin / command 共用）
# ═══════════════════════════════════════════════════════


def scan_components(messages: list[Message]) -> list[Component]:
    """
    扫描非 system 消息列表，返回从旧到新排序的连通块。

    连通块定义：以**人类** user 消息（裸文本）为分隔符，从前向后切割。
    ``⁂[kind]`` 标记的系统注入不是人类轮次 —— 不开新块（块 = 讨论主题分段，
    系统事件是主题内到达的信息）；出现在首条人类消息之前的标记消息游离于
    所有块之外（保守保留，不参与压缩）。
    每条记录中的 user_idx / terminal_idx 是 messages 中的索引（0-based）。

    每条记录：
    {
        "idx": 1,                   # 连通块编号（1-based）
        "user_idx": 0,              # messages 中的索引
        "terminal_idx": 1,          # messages 中的索引（块终点）
        "message_count": 2,         # 该连通块包含的消息总数
        "user_preview": "帮我查天气",
        "assistant_preview": "北京25°C...",
        "compressible": True/False,  # 有实际内容可压缩（msg_count > 2）
        "complete": True/False,      # 最后一条是 assistant(无 tool_calls)
        "degenerable": True/False,   # 块内存在工具调用噪音（tool 消息 / 带 tool_calls 的 assistant）
    }
    """
    # 边界 = 人类 user 消息（裸文本）。⁂[kind] 标记的系统注入不是人类轮次，不切块
    # （块 = 讨论主题分段；系统事件是主题内到达的信息）—— 见引擎.md「注入消息身份协议」。
    # 边界 = 真块起点人类 user 消息。两类 user 消息不开新块：
    # ① ⁂[kind] 系统注入（标头识别）；② _drained 忙时人类插话（submit 排队 /
    # ask_user 回答，环顶 drain 排空时打标，agent.py「环顶惰性注入」）——它们
    # 在语义上属于当前 run，切开会把前段 run 劈成不完整块（假"被用户中断"）。
    # 首条人类消息之前若只有这两类消息，仍游离于所有块之外（保守保留）。
    user_indices = [
        i
        for i, m in enumerate(messages)
        if m["role"] == "user" and not is_injected(m.get("content")) and not m.get("_drained")
    ]

    components: list[Component] = []
    for pos, user_idx in enumerate(user_indices):
        terminal_idx = user_indices[pos + 1] - 1 if pos + 1 < len(user_indices) else len(messages) - 1
        msg_count = terminal_idx - user_idx + 1
        terminal_msg = messages[terminal_idx]

        complete = terminal_msg["role"] == "assistant" and not terminal_msg.get("tool_calls")

        block = messages[user_idx : terminal_idx + 1]
        degenerable = any(m["role"] == "tool" or (m["role"] == "assistant" and m.get("tool_calls")) for m in block)

        components.append({
            "idx": pos + 1,
            "user_idx": user_idx,
            "terminal_idx": terminal_idx,
            "message_count": msg_count,
            "user_preview": messages[user_idx].get("content", "")[:120],
            "assistant_preview": terminal_msg.get("content", "")[:120],
            "compressible": msg_count > 2,
            "complete": complete,
            "degenerable": degenerable,
        })

    return components


def degenerate(
    messages: list[Message],
    targets: list[tuple[int, int]],
    protect_callsite: bool = False,
) -> tuple[bool, str, int, list[Message] | None]:
    """
    退化：将指定连通块内的工具调用链退化为纯文本 assistant 消息链。

    逐消息规则（固定，不调 LLM，块内块外同构）：
      - user（人类话语与 ⁂[kind] 注入）→ **原地保留**：抽走或删除都会让
                                        AI 独白失去刺激源（身份塌缩）
      - assistant 无 tool_calls     → 保留
      - assistant 有 tool_calls     → 先吸收其前紧邻的连续纯文本 assistant
                                      （逆序取出 → 时间正序拼接），再删除
                                      tool_calls 保留合并后的 content（转正）；
                                      既无 content 又无可吸收文本 → 整条删除
      - tool                        → 删除

    合并的动因是结构性的：消除「纯文本 assistant → assistant(tool_calls)」
    连续结构（部分 API 拒绝该结构），并把「意图文本」与它引发的调用绑在一起。
    受保护调用点（protect_callsite 且为进行中的调用点）不走上面的吸收路径——
    它本轮原样保留，改由重建阶段把它前面残留的连续纯文本并入其 content。

    Args:
        messages:         非 system 消息列表
        targets:          要退化的连通块消息索引 [(user_idx, terminal_idx), ...]
                          均为 messages 中的 0-based 索引
        protect_callsite: True 时（工具情境，agent loop 正在跑）：
                          若目标块终点是「进行中的 assistant(tool_calls)」
                          （即本次工具调用点），则终点收缩到调用点之前，
                          调用点及其即将追加的 tool 结果本轮不动，保证 loop 不断开；
                          同时将调用点前退化出的连续纯文本 assistant
                          逆序合并进调用点 content（多条合并一条）。
                          command 情境（无进行中循环）传 False。

    Returns:
        (是否成功, 描述信息, 变更的消息数, 新的消息列表或 None)
        失败时返回 (False, 错误信息, 0, None)，原始 messages 不受影响。
        变更数 = 删除的 tool/空 content 消息数 + 转正的 assistant 消息数。
    """
    targets = sorted(targets, key=lambda x: x[0])
    new_sections: list[list[Message]] = []
    processed_ends: list[int] = []
    total_changed = 0
    protected_callsites: list[int] = []

    try:
        for user_idx, terminal_idx in targets:
            eff_terminal = terminal_idx

            # 调用点保护：终点若是进行中的 assistant(tool_calls)，收缩到调用点之前。
            # 调用点（terminal_idx）本轮原样保留（含 tool_calls），等 agent 追加 tool 结果，
            # 保证 loop 不断开；下次退化时它作为历史按统一规则自然处理。
            if protect_callsite:
                while (
                    eff_terminal >= user_idx
                    and messages[eff_terminal]["role"] == "assistant"
                    and messages[eff_terminal].get("tool_calls")
                ):
                    eff_terminal -= 1
                # 收缩生效（块内确有可退化内容）→ 该终点为受保护调用点，
                # 重建时将其前的连续纯文本 assistant 逆序并入其 content（多条合并一条）。
                if eff_terminal >= user_idx:
                    protected_callsites.append(terminal_idx)

            # 无可退化内容（如块内只有 user + 调用点）→ 原样保留该块
            if eff_terminal < user_idx:
                new_sections.append([])
                processed_ends.append(user_idx - 1)
                continue

            block = messages[user_idx : eff_terminal + 1]
            kept: list[Message] = []
            for m in block:
                role = m["role"]
                if role == "tool":
                    total_changed += 1
                    continue
                if role == "user":
                    # 块内块外同构：人类话语与 ⁂[kind] 注入一律原地保留。
                    # 抽到块首会伪造对话结构，直接删则让 AI 独白失去刺激源（身份塌缩）。
                    kept.append(dict(m))
                    continue
                if role == "assistant" and m.get("tool_calls"):
                    # 调用点吸收其前紧邻的连续纯文本 assistant（逆序取出 → 时间正序拼接），
                    # 再剥 tool_calls 壳转正；无 content 且无可吸收文本 → 整条删除。
                    # 吸收边界：已转正的调用点不算「纯文本」（否则链式调用会层层吞并）。
                    absorbed: list[str] = []
                    while (
                        kept
                        and kept[-1]["role"] == "assistant"
                        and not kept[-1].get("tool_calls")
                        and not kept[-1].get(SC_CALLSITE)
                    ):
                        absorbed.insert(0, kept.pop().get("content") or "")
                    parts = [c for c in absorbed if c]
                    content = m.get("content")
                    if content:
                        parts.append(content)
                    total_changed += 1
                    if not parts:
                        continue
                    new_m = dict(m)
                    new_m.pop("tool_calls", None)
                    new_m.pop("function_call", None)
                    new_m["content"] = "\n".join(parts)
                    new_m[SC_CALLSITE] = True
                    kept.append(new_m)
                    continue
                kept.append(dict(m))

            new_sections.append(kept)
            processed_ends.append(eff_terminal)

        # ── 重建消息列表 ──
        new_messages: list[Message] = []
        i = 0
        section_idx = 0
        while i < len(messages):
            if section_idx < len(targets) and i == targets[section_idx][0]:
                for msg in new_sections[section_idx]:
                    new_messages.append(dict(msg))
                i = processed_ends[section_idx] + 1
                section_idx += 1
            elif i in protected_callsites:
                # 多条合并一条：受保护调用点前若是连续纯文本 assistant（无 tool_calls），
                # 逆序并入调用点 content。逆序 = 从最靠近调用点的文本开始逐条取出，
                # 取出的顺序即历史时间顺序（最早文本在前、调用点原文在最后），
                # 消除「纯文本 assistant → assistant(tool_calls)」连续结构。
                callsite = dict(messages[i])
                collected: list[str] = []
                # 这里不设 SC_CALLSITE 边界：受保护调用点必须吞掉其前所有无 tool_calls 的
                # assistant（含已转正调用点），否则又构成「纯文本 → 调用点」的非法连续结构。
                while (
                    new_messages and new_messages[-1]["role"] == "assistant" and not new_messages[-1].get("tool_calls")
                ):
                    collected.insert(0, new_messages[-1].get("content", ""))
                    new_messages.pop()
                if collected:
                    merged_prefix = "\n".join(c for c in collected if c)
                    original = callsite.get("content") or ""
                    if merged_prefix and original:
                        callsite["content"] = f"{merged_prefix}\n{original}"
                    elif merged_prefix:
                        callsite["content"] = merged_prefix
                new_messages.append(callsite)
                i += 1
            else:
                new_messages.append(dict(messages[i]))
                i += 1

        return (True, "degenerate completed", total_changed, new_messages)

    except Exception as e:
        return (False, f"退化失败: {e}", 0, None)


async def shortcircuit(
    messages: list[Message],
    refiner: Refiner | None,
    targets: list[tuple[int, int]],
    mode: str = "crop",
) -> tuple[bool, str, int, list[Message] | None]:
    """
    短路压缩：将指定连通块压缩为 user + assistant 消息对（一问一答）。

    语义边界（crop 是「只要首尾」的机械操作，不是「保留全部 user」）：
      一个 target 表达**一问一答**——取 target 起点那条 user 消息作请求、
      终点那条 assistant 消息作回复，中间的一切（工具调用、工具返回、AI 中间
      叙事、以及中间的 user 类消息：人类插话 / ⁂[kind] 注入回执）全部丢弃。
      target 可跨多个 run（range 语义），此时取**首 run 的 user + 末 run 的回复**，
      中间 run 整段丢弃；因此 crop 恒产出 1 对消息，且两条都是真实存在过的消息，
      不会被拼接改造成不存在的对话。

    Args:
        messages:   非 system 消息列表
        refiner:    提炼回调，(user_text, assistant_text, context_text) → (new_user, new_assistant)
                    crop 模式传 None
        targets:    要短路的连通块消息索引 [(user_idx, terminal_idx), ...]
                    均为 messages 中的 0-based 索引；每个 target 压成 1 对消息
        mode:       "regenerate" | "crop"

    Returns:
        (是否成功, 描述信息, 节省的消息数, 新的消息列表或 None)
        失败时返回 (False, 错误信息, 0, None)，原始 messages 不受影响。
    """
    targets = sorted(targets, key=lambda x: x[0])
    new_sections: list[list[Message]] = []
    total_saved = 0

    try:
        for user_idx, terminal_idx in targets:
            user_msg = messages[user_idx]
            terminal_msg = messages[terminal_idx]
            msg_count = terminal_idx - user_idx + 1
            complete = terminal_msg["role"] == "assistant" and not terminal_msg.get("tool_calls")

            # ── 构建上下文（含中文标签，命令层策略） ──
            context_parts: list[str] = []
            for j in range(user_idx, terminal_idx + 1):
                m = messages[j]
                if m["role"] == "user":
                    role_label = "系统" if is_injected(m.get("content")) else "用户"
                else:
                    role_label = "AI" if m["role"] == "assistant" else "工具"
                tc = " [调用工具]" if m.get("tool_calls") else ""
                content = (m.get("content") or "")[:500]
                context_parts.append(f"[{role_label}]{tc}: {content}")
            context_text = "\n\n".join(context_parts)

            # ── 完整块 ──
            if complete:
                # msg_count == 2 且 crop：精确保留原消息（含 tool_calls 等字段）
                if msg_count == 2 and mode == "crop":
                    new_sections.append([dict(user_msg), dict(terminal_msg)])
                # msg_count > 2 且 crop/无 refiner：截断中间消息
                elif (mode == "crop" or refiner is None) and msg_count > 2:
                    # crop：首 user 原样 + 末回复原样，中间（含中间 user 类消息）全丢
                    new_sections.append([
                        {"role": "user", "content": user_msg["content"]},
                        {"role": "assistant", "content": terminal_msg["content"]},
                    ])
                # regenerate：调 LLM 提炼
                else:
                    assert refiner is not None
                    new_user, new_assistant = await refiner(user_msg["content"], terminal_msg["content"], context_text)
                    new_sections.append([
                        {"role": "user", "content": new_user},
                        {"role": "assistant", "content": new_assistant},
                    ])
            # ── 不完整块（中断） ──
            else:
                if mode == "crop" or refiner is None:
                    new_sections.append([
                        {"role": "user", "content": user_msg.get("content", "")},
                        {"role": "assistant", "content": "被用户中断"},
                    ])
                else:
                    new_user, new_assistant = await refiner(
                        user_msg.get("content", ""),
                        "_INTERRUPTED_",
                        context_text,
                    )
                    new_sections.append([
                        {"role": "user", "content": new_user},
                        {"role": "assistant", "content": new_assistant},
                    ])

            if msg_count > 2:
                total_saved += msg_count - 2

        # ── 重建消息列表 ──
        new_messages: list[Message] = []
        i = 0
        section_idx = 0
        while i < len(messages):
            if section_idx < len(targets) and i == targets[section_idx][0]:
                for msg in new_sections[section_idx]:
                    new_messages.append(dict(msg))
                i = targets[section_idx][1] + 1
                section_idx += 1
            else:
                new_messages.append(dict(messages[i]))
                i += 1

        return (True, "shortcircuit completed", total_saved, new_messages)

    except Exception as e:
        return (False, f"短路失败: {e}", 0, None)


def _fmt_ids(ids: list[int], limit: int = 6) -> str:
    """块编号列表 → `#3,#7`（超过 limit 截断为 …）"""
    if not ids:
        return "无"
    head = ",".join(f"#{i}" for i in ids[:limit])
    return head + ("…" if len(ids) > limit else "")


def format_result(action_desc: str, before: int, after: int, remaining: list[int]) -> str:
    """压缩结果对账行（工具层与 /sc 命令层共用）。

    只报结构：动作 + 块编号、消息条数 before→after、剩余可退化块编号。
    不复述内容——模型自己看得见真实上下文，返回里放内容只会白烧 token。

    before == after 时用 ⚠️ 并写明「没剪到东西 + 下一步」，避免模型把
    「✅ … 节省 0 条」读成「已完成」而继续干别的。
    """
    saved = before - after
    verdict = "✅" if saved > 0 else "⚠️"
    line = f"{verdict} {action_desc} · 消息 {before}→{after} 条（省 {saved}）｜剩余可退化 {_fmt_ids(remaining)}"
    if saved <= 0:
        line += "\n未剪到任何消息：目标块已无中间过程/工具往返可删——换块，或换 mode（只有 degenerate 能清工具往返）"
    return line


async def execute_plan(
    messages: list[Message],
    state: Any,
    action: str,
    value: int | tuple[int, int] | list[int] | None,
    mode: str | None,
    usable_key: str,
    protect_callsite: bool,
) -> tuple[bool, str, int, list[Message] | None]:
    """
    工具层策略执行（插件 shortcircuit 专用；命令层 /sc 不经过此函数）。

    硬性策略约束（仅工具层）：
      1. 当前块（最后一个连通块）只能 degenerate（强制，不可被 crop/regenerate 覆盖）。
      2. 其他块默认 crop：未显式指定 mode（mode=None）时按 crop 处理。
      3. 序号/范围/批量指定涉及当前块时：其他块按指定行为（mode），当前块强制 degenerate。

    Args:
        messages:         非 system 消息列表
        state:            用于 regenerate 提炼（crop/degenerate 传 None 亦可）
        action:           "default" | "count" | "index" | "indices" | "range"
        value:            default=None / count=int / index=int / indices=list[int] /
                          range=(start, end)
        mode:             显式指定 mode；None = 未指定（其他块默认 crop）
        usable_key:       "degenerable" | "compressible"，default/count 目标选择的过滤字段
        protect_callsite: 工具情境传 True（agent loop 进行中，保护本次调用点）；
                          命令情境传 False（无进行中循环）

    Returns:
        (是否成功, 中文描述（对账行）, 实测条数差 = len(messages)-len(new), 新的消息列表或 None)
        失败时返回 (False, 错误信息, 0, None)，原始 messages 不受影响。

    执行顺序：先处理其他块（按指定/默认 mode），最后退化当前块
    ——当前块在历史块处理后的消息上重新定位（编号可能前移，但始终是最后一块）。
    """
    components = scan_components(messages)
    if not components:
        return (False, "没有连通块需要处理", 0, None)
    current_idx = components[-1]["idx"]

    # ── 1. 解析目标块编号（1-based） ──
    target_ids: list[int] = []
    range_merge = False  # range 语义：其他块合并为一个连通块（crop 时）

    if action in ("default", "count"):
        usable = [c for c in reversed(components) if c[usable_key]]
        if not usable:
            key_label = "可退化" if usable_key == "degenerable" else "可压缩"
            return (False, f"没有{key_label}的连通块", 0, None)
        n = 1 if action == "default" else value
        assert isinstance(n, int)
        target_ids = [c["idx"] for c in usable[:n]]
    elif action == "index":
        assert isinstance(value, int)
        if not any(c["idx"] == value for c in components):
            return (False, f"未找到编号 {value} 的连通块", 0, None)
        target_ids = [value]
    elif action == "indices":
        assert isinstance(value, list)
        for bid in value:
            if not any(c["idx"] == bid for c in components):
                return (False, f"未找到编号 {bid} 的连通块", 0, None)
        target_ids = list(value)
    elif action == "range":
        assert isinstance(value, tuple) and len(value) == 2
        start, end = value
        selected = [c for c in components if start <= c["idx"] <= end]
        if not selected:
            return (False, f"未找到编号 {start}~{end} 的连通块", 0, None)
        range_merge = True
        target_ids = [c["idx"] for c in selected]
    else:
        return (False, "未知操作", 0, None)

    if not target_ids:
        return (False, "没有可处理的连通块", 0, None)

    # ── 2. 拆分：其他块 vs 当前块（当前块强制 degenerate） ──
    has_current = current_idx in target_ids
    other_ids = [i for i in target_ids if i != current_idx]

    # ── 3. 执行其他块（指定 mode；未指定默认 crop） ──
    new_messages = list(messages)
    parts: list[str] = []

    if other_ids:
        if range_merge:
            other_comps = [c for c in components if c["idx"] in other_ids]
            other_targets = [(other_comps[0]["user_idx"], other_comps[-1]["terminal_idx"])]
        else:
            other_targets = [(c["user_idx"], c["terminal_idx"]) for c in components if c["idx"] in other_ids]
        others_mode = mode if mode is not None else "crop"
        if others_mode == "degenerate":
            ok, msg, _n, new_messages = degenerate(new_messages, other_targets, False)
        else:
            refiner: Refiner | None = None if others_mode == "crop" else build_regenerate_refiner(state)
            ok, msg, _n, new_messages = await shortcircuit(new_messages, refiner, other_targets, others_mode)
        if not ok:
            return (False, msg, 0, None)
        parts.append(f"{others_mode} {_fmt_ids(other_ids)}")

    # ── 4. 当前块强制退化（在 other 处理后的消息上重新定位最后一块） ──
    if has_current:
        assert new_messages is not None
        comps2 = scan_components(new_messages)
        if not comps2:
            return (False, "处理其他块后找不到当前块", 0, None)
        cur = comps2[-1]
        assert new_messages is not None
        ok, msg, _n, new_messages = degenerate(new_messages, [(cur["user_idx"], cur["terminal_idx"])], protect_callsite)
        if not ok:
            return (False, msg, 0, None)
        parts.append(f"degenerate #{cur['idx']}(当前块)")

    assert new_messages is not None
    after = len(new_messages)
    desc = " + ".join(parts) if parts else "无目标变化"
    # 剩余结构：排除最后一块（进行中的当前块，工具往返随调用持续产生，报它没有信息量）
    remaining = [c["idx"] for c in scan_components(new_messages)[:-1] if c["degenerable"]]
    # 第三返回值统一为「实测条数差」：degenerate 自身报的「变更数」含转正不删条，
    # 与真实节省量口径不同，对外只认 before-after 这一个真相。
    return (True, format_result(desc, len(messages), after, remaining), len(messages) - after, new_messages)


def build_regenerate_refiner(state: Any) -> Refiner:
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


# ═══════════════════════════════════════════════════════
# 命令入口
# ═══════════════════════════════════════════════════════


def parse_args(arg: str) -> tuple[str, int | tuple[int, int] | None | str, str]:
    """解析短路命令参数

    Returns:
        (action, value, mode)
        action: "list" | "default" | "count" | "index" | "range" | "error"
        value:  int | tuple(int,int) | str(错误信息)
        mode:   "degenerate" | "regenerate" | "crop"
    """
    parts = arg.strip().split()
    if not parts:
        return ("default", 1, "crop")

    clean_parts: list[str] = []
    mode = "crop"
    for p in parts:
        if p == "-c":
            mode = "crop"
        elif p == "-r":
            mode = "regenerate"
        elif p == "-d":
            mode = "degenerate"
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


async def execute(state: Any, arg: str) -> tuple[bool, str]:
    action, value, mode = parse_args(arg)

    if action == "error":
        return (True, f"❌ {value}")

    # ── 通过公共 API 获取非 system 消息 ──
    messages = state.conversation.get_non_system_messages()

    # ── /sc list ─────────────────────────────────────────
    if action == "list":
        components = scan_components(messages)
        if not components:
            return (True, "没有已完成的连通块")
        return (True, format_components_display(components))

    # ── 执行短路（命令层无硬性约束：按显式 -c/-r/-d 处理任意块，含当前块） ──
    components = scan_components(messages)
    if not components:
        return (True, "没有已完成的连通块需要短路")

    # 确定要短路的非 system 空间索引
    # 过滤条件随模式变化：degenerate 看工具噪音（degenerable），合并看内容量（compressible）
    targets: list[tuple[int, int]] = []
    target_ids: list[int] = []

    if action in ("default", "count"):
        n = 1 if action == "default" else value
        assert isinstance(n, int)
        usable_key = "degenerable" if mode == "degenerate" else "compressible"
        selected = [c for c in reversed(components) if c[usable_key]][:n]
        target_ids = [c["idx"] for c in selected]
        targets = [(c["user_idx"], c["terminal_idx"]) for c in selected]
        desc = f"{mode} {_fmt_ids(target_ids)}"
    elif action == "index":
        assert isinstance(value, int)
        for comp in components:
            if comp["idx"] == value:
                targets.append((comp["user_idx"], comp["terminal_idx"]))
                target_ids = [value]
                break
        desc = f"{mode} #{value}"
    elif action == "range":
        assert isinstance(value, tuple) and len(value) == 2
        start, end = value
        selected = [c for c in components if start <= c["idx"] <= end]
        if not selected:
            return (True, f"未找到编号 {start}~{end} 的连通块")
        min_user = selected[0]["user_idx"]
        max_terminal = selected[-1]["terminal_idx"]
        targets = [(min_user, max_terminal)]
        target_ids = [c["idx"] for c in selected]
        # 跨度语义（单 target）：crop 取首块请求 + 末块回复，中间全丢；非逐块执行
        desc = f"{mode} #{start}~#{end}（跨度：首尾截取）"
    else:
        return (True, "未知操作")

    if not targets:
        return (True, "没有可短路的连通块，或指定的连通块编号不存在")

    # ── 执行（命令层自组装，纯函数操作消息列表） ──
    if mode == "degenerate":
        # 命令情境无进行中循环，不保护调用点
        success, msg, _saved, new_messages = degenerate(messages, targets, protect_callsite=False)
    else:
        refiner: Refiner | None = None if mode == "crop" else build_regenerate_refiner(state)
        success, msg, _saved, new_messages = await shortcircuit(messages, refiner, targets, mode)

    if success:
        # 通过公共 API 写回
        state.conversation.set_messages(state.conversation.system_prompt, new_messages)
        state.session.save_context(state.conversation.to_serializable())
        assert new_messages is not None
        remaining = [c["idx"] for c in scan_components(new_messages)[:-1] if c["degenerable"]]
        return (True, format_result(desc, len(messages), len(new_messages), remaining))
    else:
        return (True, msg)


def format_components_display(components: list[Component]) -> str:
    """格式化连通块列表（markdown，结构优先）。

    一行一块：编号 / 状态 / 条数在前（固定字段，便于模型定位与选择），
    后接 80 字内容摘要——摘要必须保留，模型靠它把编号对上真实内容，
    砍太短就失去锚定作用；省 token 靠单行化（原先是每块两行 80+80 字）。
    """
    if not components:
        return "没有已完成的连通块"

    def flag_of(comp: Component) -> str:
        if comp["degenerable"]:
            return "`D`"
        if comp["message_count"] == 1 or not comp["complete"]:
            return "`*`"
        if comp["compressible"]:
            return "`C`"
        return "`~`"

    def clip(text: str, n: int = 80) -> str:
        flat = " ".join(text.split())
        if not flat:
            return "（空）"
        return flat[:n] + "…" if len(flat) > n else flat

    degenerable_ids = [c["idx"] for c in components if c["degenerable"]]
    lines: list[str] = [
        f"## 📦 连通块 共{len(components)} · 可退化{len(degenerable_ids)} · 当前块#{components[-1]['idx']}（只能退化）",
        "`D`=可退化 `C`=可压缩 `~`=已压缩 `*`=未完成｜条数=该块消息数（含工具往返）",
    ]
    for comp in components:
        lines.append(
            f"- **#{comp['idx']}** {flag_of(comp)} {comp['message_count']}条"
            f" · {clip(comp['user_preview'])} ｜ AI: {clip(comp['assistant_preview'])}"
        )
    return "\n".join(lines)
