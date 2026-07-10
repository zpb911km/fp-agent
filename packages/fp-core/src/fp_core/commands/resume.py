"""resume 命令 — 切换/删除历史会话（非交互式）

直接操作 state.session + state.conversation，不再经过 Agent 中转。

用法:
  /resume [list]            列出所有会话（默认）
  /resume latest            切换到最新会话
  /resume <sid|序号>        切换（支持 sid 或 list 中的序号）
  /resume delete <sid|序号> 删除指定会话
"""

import re

from fp_core.core.prompt_builder import PromptBuilder

name = "resume"
aliases = []
description = (
    "切换/删除历史会话。支持 sid 和 list 序号。"
    "用法: /resume [list], /resume latest, /resume <sid|序号>, /resume delete <sid|序号>"
)


def _escape_md(text: str) -> str:
    """转义 Markdown 特殊字符并替换换行，防止 summary 破坏渲染"""
    text = text.replace("\n", " ").replace("\r", "")
    escape_chars = r"\`*_{}[]()#+-.!|"
    return re.sub(rf"([{re.escape(escape_chars)}])", r"\\\1", text)


def _sorted_sessions(state, exclude_current: bool = False) -> list[tuple[str, dict]]:
    """按 updated 降序排列会话，可选排除当前会话"""
    sessions = state.session.list_sessions()
    current_sid = state.session_id
    sorted_items = sorted(
        sessions.items(),
        key=lambda x: x[1].get("updated", ""),
        reverse=True,
    )
    if exclude_current:
        return [(sid, meta) for sid, meta in sorted_items if sid != current_sid]
    return sorted_items


def _resolve_sid(state, raw: str, exclude_current: bool = False) -> str | None:
    """将用户输入解析为 sid：纯数字 → list 序号映射，否则原样返回"""
    if raw.isdigit():
        i = int(raw)
        pool = _sorted_sessions(state, exclude_current=exclude_current)
        if 1 <= i <= len(pool):
            return pool[i - 1][0]
        return None
    return raw  # 当作 sid 直接返回


def _rebuild_context(state):
    """用 system prompt 重建 context（切换会话时调用）"""
    prompt = PromptBuilder().build_system_prompt()
    state.conversation.reset(prompt)
    saved = state.session.load_context(prompt)
    if len(saved) > 1:
        state.conversation.replace_all(saved)


async def execute(state, arg: str) -> tuple[bool, str]:
    arg = arg.strip()

    # ── /resume <无参数> = /resume list ────────────────────────
    if not arg:
        arg = "list"

    # ── /resume delete <sid|序号> ─────────────────────────────
    if arg.startswith("delete "):
        sub = arg[7:].strip()
        if not sub:
            return (True, "❌ 请指定要删除的会话。查看帮助: `/help resume`")
        sid = _resolve_sid(state, sub, exclude_current=False)
        if sid is None:
            return (True, f"❌ 序号/会话 `{sub}` 无效。使用 `/resume list` 查看可用会话")
        if sid == state.session_id:
            return (True, "❌ 不能删除当前正在使用的会话")
        if state.session.delete_session(sid):
            return (True, f"🗑️ 已删除会话: `{sid}`")
        else:
            return (True, f"❌ 会话 `{sid}` 不存在。使用 `/resume list` 查看可用会话")

    if arg == "delete":
        return (True, "❌ 请指定要删除的会话。查看帮助: `/help resume`")

    # ── /resume list ───────────────────────────────────────────
    if arg == "list":
        sessions = state.session.list_sessions()
        if not sessions:
            return (True, "暂无历史会话")

        current_sid = state.session_id
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
            lines.append(f"- **[{i}]** {_escape_md(summary)} ({msg_count}条, `{sid}`){marker}")

        return (True, "\n".join(lines))

    # ── /resume latest ─────────────────────────────────────────
    if arg == "latest":
        if state.session.resume_latest():
            _rebuild_context(state)
            return (True, f"📂 已切换到最新会话: `{state.session_id}`")
        return (True, "❌ 没有可切换的会话")

    # ── /resume <sid|序号> ─────────────────────────────────────
    sid = _resolve_sid(state, arg, exclude_current=False)
    if sid is None:
        return (True, f"❌ 序号/会话 `{arg}` 无效。使用 `/resume list` 查看可用会话")
    if state.session.switch_session(sid):
        _rebuild_context(state)
        return (True, f"📂 已切换到会话: `{sid}`")
    else:
        return (True, f"❌ 会话 `{sid}` 不存在。使用 `/resume list` 查看可用会话")
