"""shortcircuit 核心 — 连通块扫描 / 退化（degenerate）/ 裁剪（crop）/ 提炼（regenerate）

工具 `shortcircuit` 与命令 `/sc` 共用本模块，**编号语义完全一致**（详见 apply_action）：

  action 缺省 = list（只列不改，最安全）
  action = degenerate | crop | regenerate 时必须给 target

命令层独有：纯数字参数 = 「最近 N 个可处理块」（/sc 3、/sc -d 2），
这是给人类省事的语法；工具层不提供——模型必须点名，选中哪块是它自己的责任。

  target 语法（宽松解析，见 parse_target）:
    "#7"        单个块
    "#3-9"      一个整体：3~9 块合成**一个** target（产物 1 对）
    "#3,#7-9"   逗号分段的，各段**分别**处理（产物 N 对）

三种动作的产物语义（一个 target = 一对消息）:
  degenerate  文字全留、结构塌缩：该段全部人类话语合成第一条 user，
              该段全部叙事文本合成第二条 assistant；⁂[kind] 工具异步输出与
              tool 消息一并丢弃（它们不是人类说话，是工具往返的一部分）。
  crop        只要首尾：该段首条 user 原文 + 末条 assistant 原文，中间全丢。
  regenerate  该段交 LLM 提炼成一对。

硬性策略（两层一致，只有「进行中调用点」因情境不同）:
  - 目标含当前块（最后一个连通块）时，该块**强制 degenerate**；
  - 工具情境（agent loop 进行中）protect_callsite=True：当前块终点若是进行中的
    调用点，该调用点本轮不动（tool_calls 要留给 loop），段内叙事并入它的 content；
  - 命令情境（/sc 到达时新块刚开、无进行中循环）protect_callsite=False。

状态标记（list）:
  D = 可退化（块内存在工具往返）
  C = 可压缩（crop 有收益，无工具往返）
  ~ = 已充分压缩（无可压缩空间）
  * = 未完成（中断块 / 待回复）
"""

import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypedDict

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
    退化：把每个 target（一段消息区间）压成「一条 user + 一条 assistant」。

    一个 target 恒产出**至多两条**消息：
      第一条 user       = 段内全部**人类话语**按时间正序合并（含 _drained 忙时插话）
      第二条 assistant  = 段内全部**叙事文本**按时间正序合并（含被剥壳的调用点 content）
    丢弃：tool 消息、assistant 的 tool_calls 壳、⁂[kind] 工具异步输出
          （注入虽以 role=user 落盘，语义上是工具往返的一部分，不是人类说话）。

    因此 range 语义（段内含多个 run）天然成立：N 个 run 合成一问一答，
    内容一条不丢，只塌结构；而 block_ids 语义（每段一块）则是每块各一对。

    Args:
        messages:         非 system 消息列表
        targets:          目标区间 [(user_idx, terminal_idx), ...]（messages 的 0-based 索引）
        protect_callsite: True 时（工具情境，agent loop 进行中）：段终点若是进行中的
                          assistant(tool_calls)，该调用点本轮原样保留（tool_calls 留给
                          loop），段内叙事并入它的 content —— 保证块最终仍只落一条
                          assistant，且不产生「纯文本 → assistant(tool_calls)」非法结构。

    Returns:
        (是否成功, 描述, 变更的消息数, 新消息列表或 None)；失败时原列表不受影响。
    """
    targets = sorted(targets, key=lambda x: x[0])
    sections: list[list[Message]] = []
    ends: list[int] = []
    total_changed = 0

    try:
        for user_idx, terminal_idx in targets:
            eff_terminal = terminal_idx
            keep_tail: list[Message] = []

            # 进行中的调用点：本轮不能改写（tool_calls 要留给 loop），留作段尾
            if protect_callsite:
                while (
                    eff_terminal >= user_idx
                    and messages[eff_terminal]["role"] == "assistant"
                    and messages[eff_terminal].get("tool_calls")
                ):
                    eff_terminal -= 1
                if eff_terminal < terminal_idx:
                    keep_tail = [dict(m) for m in messages[eff_terminal + 1 : terminal_idx + 1]]

            human_parts: list[str] = []
            ai_parts: list[str] = []
            for m in messages[user_idx : eff_terminal + 1]:
                role = m["role"]
                content = (m.get("content") or "").strip()

                if role == "tool":
                    total_changed += 1
                    continue

                if role == "user":
                    if is_injected(m.get("content")):
                        # ⁂[kind] 是工具的异步输出，与 tool 消息同级 → 丢弃
                        total_changed += 1
                        continue
                    if content:
                        human_parts.append(content)
                    else:
                        total_changed += 1
                    continue

                if role == "assistant":
                    if m.get("tool_calls"):
                        total_changed += 1  # 剥壳即变更（不论是否转正成功）
                    if content:
                        ai_parts.append(content)
                    continue

                # 理论不可达：保留其文本，避免静默丢内容
                if content:
                    ai_parts.append(content)

            merged = "\n".join(ai_parts)
            section: list[Message] = []
            if human_parts:
                section.append({"role": "user", "content": "\n".join(human_parts)})

            if keep_tail:
                first = keep_tail[0]
                original = (first.get("content") or "").strip()
                if merged and original:
                    first["content"] = f"{merged}\n{original}"
                elif merged:
                    first["content"] = merged
                section.extend(keep_tail)
            elif merged:
                section.append({"role": "assistant", "content": merged})

            sections.append(section)
            ends.append(terminal_idx)

        # ── 重建消息列表 ──
        new_messages: list[Message] = []
        i = 0
        s = 0
        while i < len(messages):
            if s < len(targets) and i == targets[s][0]:
                new_messages.extend(dict(m) for m in sections[s])
                i = ends[s] + 1
                s += 1
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
    短路压缩：将每个 target 压缩为 user + assistant 消息对（一问一答）。

    语义边界（crop 是「只要首尾」的机械操作，不是「保留全部 user」）：
      取 target 起点那条 user 消息作请求、终点那条 assistant 消息作回复，中间
      的一切（工具调用、工具返回、AI 中间叙事、以及中间的 user 类消息）全部丢弃。
      target 可跨多个 run（range 语义），此时取首 run 的 user + 末 run 的回复。

    Args:
        messages:   非 system 消息列表
        refiner:    提炼回调，(user_text, assistant_text, context_text) → (new_user, new_assistant)
                    crop 模式传 None
        targets:    目标区间 [(user_idx, terminal_idx), ...]；每个 target 压成 1 对消息
        mode:       "regenerate" | "crop"

    Returns:
        (是否成功, 描述信息, 节省的消息数, 新的消息列表或 None)
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

            # ── 构建上下文（提炼用，含中文标签） ──
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
                if msg_count == 2 and mode == "crop":
                    new_sections.append([dict(user_msg), dict(terminal_msg)])
                elif (mode == "crop" or refiner is None) and msg_count > 2:
                    new_sections.append([
                        {"role": "user", "content": user_msg["content"]},
                        {"role": "assistant", "content": terminal_msg["content"]},
                    ])
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
        line += (
            "\n未剪到任何消息：目标块已无中间过程/工具往返可删——换 target，或换 action（只有 degenerate 能清工具往返）"
        )
    return line


# ═══════════════════════════════════════════════════════
# target 解析（工具与命令共用）
# ═══════════════════════════════════════════════════════

# 宽松匹配：可选 #／＃ 前缀；分隔符 - ~ — – 等价
_TARGET_SEG = re.compile(r"^#?(\d+)(?:-#?(\d+))?$")

_TARGET_HINT = "写法：#7 单块 · #3-9 整体（合成一对）· #3,#7-9 逗号分段（各处理各的）"


def parse_target(spec: str) -> tuple[list[tuple[int, int]] | None, str | None]:
    """解析 target 字符串 → 段列表 [(start, end), ...]，失败返回 (None, 错误信息)。

    段内 `-`（或 ~ — –）表示**一个整体**，段间逗号/空白表示**分别处理**。
    宽松接受 `7`、`#7`、`＃7`、`#3-#9`、全角逗号、空格。
    """
    text = (spec or "").strip().replace("＃", "#")
    if not text:
        return (None, "target 为空")

    # 先归一化区间分隔符（含两侧空白）：~ — – 与 - 等价 → 再按逗号/空白切段。
    # 顺序不能反：先切空白会把 "#3 - 9" 切成 ["#3","-","9"]。
    text = re.sub(r"\s*[-~—–]\s*", "-", text)

    parts = [p for p in re.split(r"[,，;；\s]+", text) if p]
    segments: list[tuple[int, int]] = []
    for part in parts:
        m = _TARGET_SEG.match(part)
        if m is None:
            return (None, f"无法解析「{part}」")
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) is not None else start
        if start < 1 or end < 1:
            return (None, f"编号必须 ≥ 1：「{part}」")
        if start > end:
            return (None, f"起始 #{start} 大于终止 #{end}")
        segments.append((start, end))
    return (segments, None)


def fmt_segments(segments: list[tuple[int, int]]) -> str:
    """段列表 → `#3-9` / `#3,#7-9`（对账行用）"""
    return ",".join(f"#{s}" if s == e else f"#{s}-{e}" for s, e in segments)


# ═══════════════════════════════════════════════════════
# 执行（工具层 / 命令层共用）
# ═══════════════════════════════════════════════════════


async def run_plan(
    messages: list[Message],
    state: Any,
    action: str,
    segments: list[tuple[int, int]],
    protect_callsite: bool,
) -> tuple[bool, str, int, list[Message] | None]:
    """按段执行：每段一个 target（段内整体处理），段间分别处理。

    Args:
        action:           "degenerate" | "crop" | "regenerate"
        segments:         [(start, end), ...]（1-based 块编号区间）
        protect_callsite: 工具情境 True（当前块进行中的调用点不剥离）；
                          命令情境 False

    当前块（最后一个连通块）恒走 degenerate —— 它可能正被 loop 使用，
    crop/regenerate 会破坏进行中的结构。
    """
    components = scan_components(messages)
    if not components:
        return (False, "没有连通块需要处理", 0, None)
    current_idx = components[-1]["idx"]

    # 每段 → (action, 块编号列表, 是否为当前块那一段)
    plans: list[tuple[str, list[int], bool]] = []
    for start, end in segments:
        ids = [c["idx"] for c in components if start <= c["idx"] <= end]
        if not ids:
            return (False, f"未找到编号 #{start}-#{end} 的连通块", 0, None)
        # 当前块恒走 degenerate。请求的动作本来就是 degenerate 时不拆段——
        # 拆开会把 range 的整体合成一对拆成多对（违背段粒度语义）。
        if action != "degenerate" and current_idx in ids:
            others = [i for i in ids if i != current_idx]
            if others:
                plans.append((action, others, False))
            plans.append(("degenerate", [current_idx], True))
        else:
            plans.append((action, ids, current_idx in ids))

    cur: list[Message] = list(messages)
    # 倒序执行：段合并会减少块数，先处理靠后的段才不会让前面段的块编号前移失效
    for pmode, ids, is_current in reversed(plans):
        by_idx = {c["idx"]: c for c in scan_components(cur)}
        sel = [by_idx[i] for i in ids if i in by_idx]
        if not sel:
            return (False, "处理后重新定位目标块失败", 0, None)
        # 关键：一段 = **一个** target（首块起点 → 末块终点）。
        # 逐块展开会抹掉「- 是一个整体、, 是分别处理」的粒度差异。
        spans = [(sel[0]["user_idx"], sel[-1]["terminal_idx"])]

        if pmode == "degenerate":
            ok, msg, _n, new_messages = degenerate(cur, spans, protect_callsite and is_current)
        else:
            refiner: Refiner | None = None if pmode == "crop" else build_regenerate_refiner(state)
            ok, msg, _n, new_messages = await shortcircuit(cur, refiner, spans, pmode)

        if not ok or new_messages is None:
            return (False, msg, 0, None)
        cur = new_messages

    desc = f"{action} {fmt_segments(segments)}"
    after = len(cur)
    # 剩余结构：排除最后一块（进行中的当前块，工具往返随调用持续产生，报它没有信息量）
    remaining = [c["idx"] for c in scan_components(cur)[:-1] if c["degenerable"]]
    return (True, format_result(desc, len(messages), after, remaining), len(messages) - after, cur)


async def apply_action(
    messages: list[Message],
    state: Any,
    action: str,
    target: str,
    protect_callsite: bool,
) -> tuple[bool, str]:
    """工具层与命令层**唯一**执行入口 —— 两层语义在此对齐。

    action 缺省/"list" → 只列不改（target 被忽略，附一行提示）
    其余 action       → target 必填；解析失败时返回错误 + 当前 list，
                       让模型一次调用就能改对（不必再花一次 list）

    成功时写回会话并返回对账行。
    """
    if action == "list":
        listing = format_components_display(scan_components(messages))
        if (target or "").strip():
            listing += "\n（action=list 只列不改；要修剪请显式指定 action=degenerate/crop/regenerate）"
        return (True, listing)

    segments, err = parse_target(target)
    if err is not None or segments is None:
        reason = err or "target 为空"
        return (True, f"❌ {reason}｜{_TARGET_HINT}\n{format_components_display(scan_components(messages))}")

    ok, msg, _saved, new_messages = await run_plan(messages, state, action, segments, protect_callsite)
    if not ok or new_messages is None:
        return (False, msg)

    state.conversation.set_messages(state.conversation.system_prompt, new_messages)
    state.session.save_context(state.conversation.to_serializable())
    return (True, msg)


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
# 命令入口（/sc）
# ═══════════════════════════════════════════════════════

_ACTION_FLAGS = {
    "-d": "degenerate",
    "--degenerate": "degenerate",
    "-c": "crop",
    "--crop": "crop",
    "-r": "regenerate",
    "--regenerate": "regenerate",
    "list": "list",
}


def parse_command(arg: str) -> tuple[str, str, int | None]:
    """解析 /sc 参数 → (action, target, count)。

    action: list | degenerate | crop | regenerate（缺省 crop）
    target: 块编号表达式，语法与工具层完全一致（#7 / #3-9 / #3,#7-9）
    count : 纯数字 = 「最近 N 个可处理块」——**命令层独有**的便利语法，
            工具层没有（模型点名即处理，不猜）。

      /sc                → (crop, "", 1)          最近 1 个可压缩块
      /sc 3              → (crop, "", 3)          最近 3 个可压缩块
      /sc -d 2           → (degenerate, "", 2)    最近 2 个有工具往返的块
      /sc #3-9           → (crop, "#3-9", None)   整段一对
      /sc -r #1-3        → (regenerate, "#1-3", None)
      /sc list           → (list, "", None)
    """
    action: str | None = None
    target_parts: list[str] = []
    count: int | None = None
    for token in (arg or "").split():
        flag = _ACTION_FLAGS.get(token.lower())
        if flag is not None:
            action = flag
            continue
        # 纯数字 = 计数；带 # / 连字符 / 逗号的 = 编号表达式；其余交给 parse_target 报错
        if token.isdigit():
            count = int(token)
        else:
            target_parts.append(token)
    if action is None:
        action = "crop"
    if action == "list":
        return ("list", "", None)
    if count is None and not target_parts:
        count = 1
    return (action, " ".join(target_parts), count)


async def apply_count(
    messages: list[Message],
    state: Any,
    action: str,
    n: int,
    protect_callsite: bool = False,
) -> tuple[bool, str]:
    """命令层专用：「最近 N 个可处理块」逐个处理（N 对）。

    可处理 = degenerate 看 `degenerable`（有工具往返），crop/regenerate 看
    `compressible`（条数 >2 且结构非最简）——这是命令层保留的便利语法；
    工具层不提供（模型点名即处理，选中什么是它自己的责任）。
    """
    if n < 1:
        return (True, f"❌ 数量必须 ≥1（收到 {n}）")
    components = scan_components(messages)
    usable_key: Literal["degenerable", "compressible"] = "degenerable" if action == "degenerate" else "compressible"
    usable = [c for c in components if c[usable_key]]
    if not usable:
        what = "可退化" if action == "degenerate" else "可压缩"
        return (True, f"没有{what}的连通块｜改用编号点名\n{format_components_display(components)}")
    segments = [(c["idx"], c["idx"]) for c in usable[-n:]]
    ok, msg, _saved, new_messages = await run_plan(messages, state, action, segments, protect_callsite)
    if not ok or new_messages is None:
        return (False, msg)
    state.conversation.set_messages(state.conversation.system_prompt, new_messages)
    state.session.save_context(state.conversation.to_serializable())
    return (True, msg)


async def execute(state: Any, arg: str) -> tuple[bool, str]:
    """命令层入口：计数语法走 apply_count，编号语法走 apply_action（与工具同源）。"""
    action, target, count = parse_command(arg)
    messages = state.conversation.get_non_system_messages()
    # 命令到达时新块刚开、无进行中循环 → 不保护调用点
    if count is not None and target.strip():
        return (True, "❌ 计数与编号不能同时给：/sc 3 或 /sc #3-9，二选一")
    if count is not None:
        _ok, msg = await apply_count(messages, state, action, count, protect_callsite=False)
        return (True, msg)
    _ok, msg = await apply_action(messages, state, action, target, protect_callsite=False)
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
