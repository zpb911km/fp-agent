"""会话视图层（列表渲染 / 元数据筛选 / 检索结果渲染）

放在 core（L1）而非 commands（L2）的原因：命令之间不允许互相依赖
（R2：L2 跨单元互依 = 错误），而 `/resume` 的降级搜索（`-f`）同样需要这套渲染。
把共用渲染下沉到内核，两个命令都只依赖 L1，分层才自洽。

下方 `会话列表的共用视图逻辑（会话列表 / 筛选 / 检索结果共用）

拆出本模块的原因：``/resume list`` 与 ``/resume -f`` 必须共享**同一个排序池**。
序号是用户切换会话的唯一快捷键（``/resume 7``），若两处各自排序，
「列表里显示 [12]、敲 12 却切到别的会话」——这是最难察觉的一类 bug。
所以排序与序号解析只有一份实现，两个命令都从这里取。
"""

import re
from typing import Any, cast

from fp_core.core.state import State


def escape_md(text: str) -> str:
    """转义 Markdown 特殊字符并替换换行，防止 summary 破坏渲染"""
    text = text.replace("\n", " ").replace("\r", "")
    escape_chars = r"\`*_{}[]()#+-.!|"
    return re.sub(rf"([{re.escape(escape_chars)}])", r"\\\1", text)


def kw_suffix(meta: dict[str, Any]) -> str:
    """关键词后缀：`` · 量子 退火 排程``（无关键词返回空串）。

    只读 ``meta["kw"]``——由会话落盘时的 KL 管线预先算好，这里绝不算。
    """
    kws = meta.get("kw")
    if not isinstance(kws, list) or not kws:
        return ""
    words = [str(w) for w in cast(list[Any], kws)[:3] if w]
    return (" · " + " ".join(words)) if words else ""


def matches(meta: dict[str, Any], terms: list[str], sid: str = "") -> bool:
    """元数据筛选：关键词 / 标题 / sid / cwd 中命中**全部**词（AND，大小写不敏感）。

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
    return all(t.casefold() in haystack for t in terms)


def sorted_sessions(state: State, exclude_current: bool = False) -> list[tuple[str, dict[str, Any]]]:
    """按 updated 降序排列会话（**序号池的唯一来源**）"""
    sessions = state.session.list_sessions()
    current_sid = state.session_id
    items = sorted(
        sessions.items(),
        key=lambda x: x[1].get("updated", ""),
        reverse=True,
    )
    if exclude_current:
        return [(sid, meta) for sid, meta in items if sid != current_sid]
    return items


def session_list_text(state: State, *, main_only: bool = False, terms: list[str] | None = None) -> str:
    """会话列表渲染的唯一实现（list / --main / 带筛选共用）。

    序号**始终基于全量列表**（与 ``resolve_sid`` 的解析池一致）：筛选和
    ``--main`` 只隐藏条目、不重编号，因此 ``/resume 7`` 在任何视图下都指向
    同一个会话——序号不连续属正常。
    """
    sorted_items = sorted_sessions(state)
    if not sorted_items:
        return "暂无历史会话"

    current_sid = state.session_id
    items: list[tuple[int, str, dict[str, Any]]] = []
    for i, (sid, meta) in enumerate(sorted_items, 1):
        if main_only and meta.get("source") == "subagent":
            continue
        if terms and not matches(meta, terms, sid):
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
        "`/resume list <词>` 按关键词/标题筛选，`/resume -f <词>` 搜正文内容，`/resume --main` 只看主会话",
        "（列表从大到小打印：越靠下越新，**[1] = 最新会话**）",
    ]

    lines = list(header)
    # 倒序打印：最新的 [1] 落在输出末尾（紧邻输入提示行，可见）
    for i, sid, meta in reversed(items):
        summary = meta.get("summary", "") or "(无摘要)"
        msg_count = meta.get("message_count", 0)
        marker = " ⬅" if sid == current_sid else ""
        tag = "🔹 " if meta.get("source") == "subagent" else ""
        lines.append(f"- **[{i}]** {tag}{escape_md(summary)}{kw_suffix(meta)} ({msg_count}条, `{sid}`){marker}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# 检索结果渲染（/resume -f 与降级路径共用）
# ══════════════════════════════════════════════════════════

# 搜索结果最多列出多少个会话（序号仍基于全量池，不重编号）
DISPLAY_LIMIT = 20
ROLE_ICON = {"user": "👤", "assistant": "🤖"}


def _clean(text: str) -> str:
    """片段清洗：放进行内代码，避免 Markdown 标记污染列表渲染。

    片段是给人「扫一眼确认是不是这条」用的，转义成一堆反斜杠反而全是噪声——
    换成反引号包裹 + 去掉冲突字符最易读。
    """
    flat = " ".join(text.split())
    return flat.replace("`", "′").replace("|", "丨")


def render_search(state: State, terms: list[str]) -> str:
    """正文检索结果渲染（含命中片段）"""
    from fp_core.core.session_search import search_sessions

    words = [t.strip() for t in terms if t.strip()]
    if not words:
        return "❌ 请给出要搜索的词。用法: `/resume -f <词...>`（多词之间为 AND）"

    pool = sorted_sessions(state)
    if not pool:
        return "暂无历史会话"

    body_hits = {r["sid"]: r for r in search_sessions(words, limit=len(pool) + 1)}
    index = {sid: i for i, (sid, _) in enumerate(pool, 1)}
    current_sid = state.session_id

    # 正文命中 ∪ 仅元数据命中；顺序沿用排序池 = 更新时间降序
    entries: list[tuple[int, dict[str, Any], list[dict[str, str]]]] = []
    meta_only = 0
    for sid, meta in pool:
        if sid in body_hits:
            entries.append((index[sid], meta, body_hits[sid]["snippets"]))
        elif matches(meta, words, sid):
            entries.append((index[sid], meta, []))
            meta_only += 1

    if not entries:
        return f"🔍 没有找到含 `{' '.join(words)}` 的会话（已搜索 {len(pool)} 个会话的正文与标题）"

    lines = [
        f"## 🔍 `{' '.join(words)}` — 命中 {len(entries)} 个会话（正文 {len(body_hits)}，仅标题/关键词 {meta_only}）",
        "用 `/resume <序号>` 直接切换；序号与 `/resume list` 一致",
    ]
    for i, meta, snippets in entries[:DISPLAY_LIMIT]:
        summary = meta.get("summary", "") or "(无摘要)"
        marker = " ⬅" if meta.get("id") == current_sid else ""
        tag = "🔹 " if meta.get("source") == "subagent" else ""
        lines.append(
            f"- **[{i}]** {tag}{escape_md(summary)}{kw_suffix(meta)} ({meta.get('message_count', 0)}条){marker}"
        )
        for snip in snippets:
            icon = ROLE_ICON.get(snip["role"], "·")
            lines.append(f"      {icon} `{_clean(snip['text'])}`")
    if len(entries) > DISPLAY_LIMIT:
        lines.append(f"… 另有 {len(entries) - DISPLAY_LIMIT} 个命中未列出，缩小查询范围（加词）可精确定位")
    return "\n".join(lines)
