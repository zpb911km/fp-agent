"""compact 命令 — 压缩对话历史（异步）

自包含实现，仅通过 ConversationState 公共 API 操作消息。
core 层只提供 llm.summarize() 基础设施，压缩策略完全由本命令层控制。
"""

from dataclasses import dataclass

from fp_core import display

name = "compact"
aliases: list[str] = []
description = "压缩对话历史"


@dataclass
class _CompactConfig:
    """压缩配置（命令层私有）"""

    keep_meaningful: int = 4  # 保留的有意义（user/assistant）消息数


def _find_split(history: list[dict], keep: int) -> int | None:
    """从尾部向前扫描，找到第 keep 条 user/assistant 消息的位置"""
    meaningful_found = 0
    for i in range(len(history) - 1, -1, -1):
        if history[i]["role"] in ("user", "assistant"):
            meaningful_found += 1
            if meaningful_found == keep:
                return i
    return None


def _format_for_summary(messages: list[dict]) -> str:
    """将待压缩消息格式化为 LLM 摘要输入文本（策略点：模板/截断/语言标签）"""
    parts = []
    for m in messages:
        role = m["role"]
        if role == "user":
            label = "User"
        elif role == "assistant":
            label = "Assistant"
        else:
            label = "Tool"
        content = (m.get("content") or "")[:300]
        parts.append(f"[{label}]: {content}")
    return "\n\n".join(parts)


async def execute(state, arg: str) -> tuple[bool, str]:
    conv = state.conversation
    config = _CompactConfig()

    # ── 1. 判断是否需要压缩 ──
    history = conv.get_non_system_messages()
    if len(history) <= config.keep_meaningful:
        return (True, "对话历史较短，无需压缩")

    # ── 2. 切割（策略：保留尾部 keep_meaningful 条 user/assistant） ──
    split_idx = _find_split(history, config.keep_meaningful)
    if split_idx is None:
        return (True, "对话历史较短，无需压缩")

    to_compact = history[:split_idx]
    recent = history[split_idx:]

    if not to_compact:
        return (True, "无需压缩")

    # ── 3. 格式化 + 调用 LLM 生成摘要 ──
    display.info("🔄 正在压缩对话历史...")

    compact_text = _format_for_summary(to_compact)
    try:
        summary = await state.llm.summarize(compact_text)
        summary = (summary or "").strip()
    except Exception as e:
        display.error(f"  压缩失败: {e}")
        return (False, f"压缩失败: {e}")

    if not summary:
        return (False, "压缩失败：摘要为空")

    # ── 4. 重建上下文（策略：摘要以 system 角色插入） ──
    summary_message = {
        "role": "system",
        "content": (f"以下是压缩后的对话历史摘要（省略了 {len(to_compact)} 条早期消息）：\n{summary}"),
    }

    conv.set_messages(conv.system_prompt, [summary_message, *recent])

    # ── 5. 持久化 ──
    state.session.save_context(conv.to_serializable())

    display.info(f" ✅\n📦 已压缩 {len(to_compact)} 条早期消息为摘要，保留 {len(recent)} 条")
    return (True, "**📦 历史已压缩**")
