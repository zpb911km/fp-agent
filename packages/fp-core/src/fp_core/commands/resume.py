"""resume 命令 — 切换/删除/清理历史会话（非交互式）

直接操作 state.session + state.conversation，不再经过 Agent 中转。

用法:
  /resume [list]            列出所有会话（默认）
  /resume latest            切换到最新会话
  /resume <sid|序号>        切换（支持 sid 或 list 中的序号）
  /resume delete <sid|序号> 删除指定会话
  /resume prune             清理 0 长度会话文件（移到 .trash/，不真删）
"""

import re
from typing import Any, cast

from fp_core.core.state import State

name = "resume"
aliases: list[str] = []
description = (
    "切换/删除/清理历史会话。支持 sid 和 list 序号，可按关键词筛选。"
    "用法: /resume [list] [词...], /resume latest, /resume <sid|序号>, "
    "/resume --main [词...], /resume delete <sid|序号>, /resume prune"
)


def _escape_md(text: str) -> str:
    """转义 Markdown 特殊字符并替换换行，防止 summary 破坏渲染"""
    text = text.replace("\n", " ").replace("\r", "")
    escape_chars = r"\`*_{}[]()#+-.!|"
    return re.sub(rf"([{re.escape(escape_chars)}])", r"\\\1", text)


def _kw_suffix(meta: dict[str, Any]) -> str:
    """关键词后缀：`` · 量子 退火 排程``（无关键词返回空串）。

    只读 ``meta["kw"]``——由会话落盘时的 KL 管线预先算好，这里绝不算。
    """
    kws = meta.get("kw")
    if not isinstance(kws, list) or not kws:
        return ""
    words = [str(w) for w in cast(list[Any], kws)[:3] if w]
    return (" · " + " ".join(words)) if words else ""


def _matches(meta: dict[str, Any], terms: list[str], sid: str = "") -> bool:
    """筛选：关键词 / 标题 / sid / cwd 中命中**全部**词（AND，大小写不敏感）。

    关键词入检索是这套索引的意义所在——标题常常只是「推送」「ok」，
    而关键词记着这个会话到底在干什么。
    """
    kws = meta.get("kw")
    haystack = " ".join([
        str(meta.get("summary") or ""),
        " ".join(str(w) for w in cast(list[Any], kws)) if isinstance(kws, list) else "",
        sid or str(meta.get("id") or ""),
        str(meta.get("cwd") or ""),
    ]).lower()
    return all(term.lower() in haystack for term in terms)


def _session_list_text(state: State, *, main_only: bool = False, terms: list[str] | None = None) -> str:
    """会话列表渲染的唯一实现（list / --main / 带筛选共用）。

    序号**始终基于全量列表**（与 `_resolve_sid` 的解析池一致）：筛选和
    `--main` 只隐藏条目、不重编号，因此 `/resume 7` 在任何视图下都指向
    同一个会话——序号不连续属正常。
    """
    sessions = state.session.list_sessions()
    if not sessions:
        return "暂无历史会话"

    current_sid = state.session_id
    sorted_items = sorted(
        sessions.items(),
        key=lambda x: x[1].get("updated", ""),
        reverse=True,
    )

    items: list[tuple[int, str, dict[str, Any]]] = []
    for i, (sid, meta) in enumerate(sorted_items, 1):
        if main_only and meta.get("source") == "subagent":
            continue
        if terms and not _matches(meta, terms, sid):
            continue
        items.append((i, sid, meta))

    if not items:
        if terms:
            return f"🔍 没有匹配 `{' '.join(terms)}` 的会话（共 {len(sorted_items)} 个）"
        return "没有主会话（当前全部为 subagent 子任务会话）"

    if main_only:
        header = ["## 📂 主会话列表（已过滤 subagent，序号不连续属正常）"]
    elif terms:
        header = [f"## 🔍 筛选 `{' '.join(terms)}`：{len(items)}/{len(sorted_items)} 个会话"]
    else:
        header = ["## 📂 会话列表"]
    header += [
        "使用 `/resume <sid|序号>` 切换，`/resume latest` 切换到最新",
        "`/resume list <词>` 按关键词/标题筛选，`/resume --main` 只看主会话",
        "（列表从大到小打印：越靠下越新，**[1] = 最新会话**）",
    ]

    lines = list(header)
    # 倒序打印：最新的 [1] 落在输出末尾（紧邻输入提示行，可见）
    for i, sid, meta in reversed(items):
        summary = meta.get("summary", "") or "(无摘要)"
        msg_count = meta.get("message_count", 0)
        marker = " ⬅" if sid == current_sid else ""
        tag = "🔹 " if meta.get("source") == "subagent" else ""
        lines.append(f"- **[{i}]** {tag}{_escape_md(summary)}{_kw_suffix(meta)} ({msg_count}条, `{sid}`){marker}")
    return "\n".join(lines)


def _sorted_sessions(state: State, exclude_current: bool = False) -> list[tuple[str, dict[str, Any]]]:
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


def _resolve_sid(state: State, raw: str, exclude_current: bool = False) -> str | None:
    """将用户输入解析为 sid：纯数字 → list 序号映射，否则原样返回"""
    if raw.isdigit():
        i = int(raw)
        pool = _sorted_sessions(state, exclude_current=exclude_current)
        if 1 <= i <= len(pool):
            return pool[i - 1][0]
        return None
    return raw  # 当作 sid 直接返回


def _rebuild_context(state: State) -> None:
    """用 system prompt 重建 context（切换会话时调用）"""
    state.rebuild_system_prompt()
    prompt = state.conversation.system_prompt
    saved = state.session.load_context(prompt)
    if saved:
        state.conversation.set_messages(prompt, saved)
    else:
        state.conversation.set_system_prompt(prompt)


async def execute(state: State, arg: str) -> tuple[bool, str]:
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

    # ── /resume prune：清理 0 长度会话文件 ──────────────────────
    if arg in ("prune", "clean"):
        from fp_core.core.session import session_trash_dir

        moved = state.session.prune_empty_sessions()
        trash = session_trash_dir()
        if not moved:
            return (True, "✅ 没有可清理的空会话（正文没有任何消息行的会话文件）")
        lines = [
            f"🧹 已清理 {len(moved)} 个空会话（正文没有任何消息行）",
            f"   文件移动到 `{trash}`，未删除，需要可手工搬回：",
        ]
        for sid in moved[:20]:
            lines.append(f"   - `{sid}`")
        if len(moved) > 20:
            lines.append(f"   … 另有 {len(moved) - 20} 个")
        return (True, "\n".join(lines))

    # ── /resume list [筛选词] ──────────────────────────────────
    if arg == "list" or arg.startswith("list "):
        terms = arg[5:].split() if arg.startswith("list ") else []
        return (True, _session_list_text(state, terms=terms))

    # ── /resume --main [筛选词]：只看主会话（过滤 subagent） ───
    if arg in ("--main", "main") or arg.startswith(("--main ", "main ")):
        rest = arg.split(" ", 1)
        terms = rest[1].split() if len(rest) > 1 else []
        return (True, _session_list_text(state, main_only=True, terms=terms))

    # ── /resume latest ─────────────────────────────────────────
    if arg == "latest":
        # 保存当前会话的上下文和摘要，再切换
        state.session.save_and_summarize(state.conversation.to_serializable())
        if state.session.resume_latest():
            _rebuild_context(state)
            return (True, f"📂 已切换到最新会话: `{state.session_id}`")
        return (True, "❌ 没有可切换的会话")

    # ── /resume <sid|序号> ─────────────────────────────────────
    sid = _resolve_sid(state, arg, exclude_current=False)
    if sid is None:
        return (True, f"❌ 序号/会话 `{arg}` 无效。使用 `/resume list` 查看可用会话")
    # 保存当前会话的上下文和摘要，再切换
    state.session.save_and_summarize(state.conversation.to_serializable())
    if state.session.switch_session(sid):
        _rebuild_context(state)
        # 落盘新会话：启动时按 meta.updated 取「最新会话」，而上面刚把【旧】会话的
        # updated 刷成了当前时刻 —— 不落盘新会话，崩溃窗口内重启会回退到旧会话，
        # 本次 /resume 静默失效。与 back/compact/fork 对称：改 ctx 者自落盘。
        state.session.save_context(state.conversation.to_serializable())
        return (True, f"📂 已切换到会话: `{sid}`")
    else:
        return (True, f"❌ 会话 `{sid}` 不存在。使用 `/resume list` 查看可用会话")
