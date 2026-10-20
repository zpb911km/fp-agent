"""resume 命令 — 切换/删除/清理历史会话（非交互式）

直接操作 state.session + state.conversation，不再经过 Agent 中转。

用法:
  /resume [list]            列出所有会话（默认）
  /resume latest            切换到最新会话
  /resume <sid|序号>        切换（支持 sid 或 list 中的序号）
  /resume delete <sid|序号> 删除指定会话
  /resume prune             清理 0 长度会话文件（移到 .trash/，不真删）
  /resume -f <词...>        按**正文内容**搜索（扫全库约 300ms，多词 AND）
"""

import os

from fp_core.core.session_view import render_search, session_list_text, sorted_sessions
from fp_core.core.state import State

name = "resume"
aliases: list[str] = []
description = (
    "切换/删除/清理历史会话。支持 sid 和 list 序号，可按关键词筛选。"
    "用法: /resume [list] [词...], /resume latest, /resume <sid|序号>, "
    "/resume -f <词...> 搜正文, /resume --main [词...], "
    "/resume delete <sid|序号>, /resume prune"
)


def _resolve_sid(state: State, raw: str, exclude_current: bool = False) -> str | None:
    """将用户输入解析为 sid：纯数字 → list 序号映射，否则原样返回"""
    if raw.isdigit():
        i = int(raw)
        pool = sorted_sessions(state, exclude_current=exclude_current)
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
        return (True, session_list_text(state, terms=terms))

    # ── /resume -f <词...>：按正文内容搜索 ────────────────────
    if arg == "-f" or arg.startswith("-f "):
        rest = arg.split(None, 1)
        terms = rest[1].split() if len(rest) > 1 else []
        if not terms:
            return (True, "❌ 请给出要搜索的词。用法: `/resume -f <词...>`（多词之间为 AND）")
        return (True, render_search(state, terms))

    # ── /resume --main [筛选词]：只看主会话（过滤 subagent） ───
    if arg in ("--main", "main") or arg.startswith(("--main ", "main ")):
        rest = arg.split(" ", 1)
        terms = rest[1].split() if len(rest) > 1 else []
        return (True, session_list_text(state, main_only=True, terms=terms))

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
        total = len(sorted_sessions(state))
        return (True, f"❌ 序号 `{arg}` 超出范围（共 {total} 个会话）。用 `/resume list` 查看")
    # 判据：list_sessions() 命中，或**会话文件实际存在**。后者不可省——缓存/
    # 未刷新的列表会漏掉刚建的会话，把合法 sid 误当成搜索词。文件在 = 是会话。
    known = sid in state.session.list_sessions()
    path_getter = getattr(state.session, "get_session_path", None)
    if not known and path_getter is not None:
        try:
            known = os.path.exists(path_getter(sid))
        except OSError:
            known = False
    if not known:
        # 既不是序号也不是任何存在的会话 —— 用户多半是在**查找**（敲了想找的词）。
        # 降级为正文搜索，而不是甩一句「会话不存在」让他自己猜语法。
        # 显式形式是 /resume -f <词...>；这里只是省掉 -f 的便利路径。
        return (True, render_search(state, arg.split()))

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
