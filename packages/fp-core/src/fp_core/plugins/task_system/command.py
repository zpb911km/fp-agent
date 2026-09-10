"""task 命令模块 — /task

由 task_system 插件在 ON_INIT 时通过 register_command 注入（不走 commands/ 目录自动发现）。

面向用户的**控制台**（不是"看图命令"）：
- 看: list / show / view
- 开: new
- 清: clear
- **人的特权**: approve(批) / reject(打回) / answer(答) / pause·resume·abort(停)

渲染分工:
- 终端(`frontend=terminal`)画不了 mermaid → 出树形文本(代码围栏, 保换行)。
- 其余(webui/acp/rest) → 出 md 内嵌 mermaid, 由各显示模块渲染。
"""

from __future__ import annotations

import time
from typing import Any

from fp_core.taskmap.models import TERMINAL_MAP_STATUSES, MapStatus, NodeStatus, TaskMap
from fp_core.taskmap.present import to_markdown, to_tree
from fp_core.taskmap.render import MAP_LABELS, render_full
from fp_core.taskmap.store import TaskMapStore

name = "task"
description = (
    "任务图控制台。用法: /task [list] · /task show <id> · /task view <id> [--mermaid|--tree] · "
    "/task new <标题> · /task approve|reject <id> [原因] · /task answer <id> <q#> <文本> · "
    "/task pause|resume|abort <id> · /task clear"
)

_HELP = """**/task — 任务图控制台**

| 子命令 | 作用 |
|---|---|
| `/task` · `/task list` | 列出所有任务 |
| `/task show <id>` | 全文(契约/节点/边/待澄清) |
| `/task view <id> [--mermaid\\|--tree]` | 看图(终端默认树, 其余默认图) |
| `/task new <标题>` | 新建任务 |
| `/task approve <id>` | **批准交付** → completed |
| `/task reject <id> [原因]` | **打回** → active(原因记入待办) |
| `/task answer <id> <q#> <文本>` | **回应待澄清**(解 agent 的 blocked) |
| `/task pause <id>` · `/task resume <id>` | **暂停 / 恢复** |
| `/task abort <id>` | **作废** → superseded |
| `/task clear` | 清除终态任务 |

> 人特有的权力: **批、答、停** —— 交付闸门 / 回应阻塞 / 自治安全阀。"""


# ═══════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════


def _fence(text: str, lang: str = "") -> str:
    """包进代码围栏 —— 防 markdown 折叠换行(终端 rich 按 markdown 渲染)。"""
    return f"```{lang}\n{text}\n```"


def _frontend(state: Any) -> str:
    io = getattr(state, "io", None)
    return str(getattr(io, "frontend", "unknown")) if io is not None else "unknown"


def _label(m: TaskMap) -> str:
    return MAP_LABELS.get(m.status.value, m.status.value)


def _pick(store: TaskMapStore, id_str: str | None) -> tuple[TaskMap | None, str | None]:
    """按 id 取; 省略 id 且只有一个任务时取之。返回 (map, 错误文案)。"""
    if id_str:
        m = store.get(id_str)
        if m is None:
            return None, f"未找到任务 #{id_str}（用 `/task list` 查看）"
        return m, None
    maps = store.list_all()
    if not maps:
        return None, "暂无任务。用 `/task new <标题>` 新建。"
    if len(maps) == 1:
        return maps[0], None
    ids = ", ".join(f"#{m.id}" for m in sorted(maps, key=lambda x: x.id))
    return None, f"有多个任务，请指定 id：{ids}"


def _status_guard(m: TaskMap, allowed: tuple[MapStatus, ...], verb: str) -> str | None:
    if m.status not in allowed:
        allow = "/".join(MAP_LABELS.get(s.value, s.value) for s in allowed)
        return f"⚠ 任务 `#{m.id}` 当前状态 [{_label(m)}]，只有 [{allow}] 才能 {verb}。"
    return None


# ═══════════════════════════════════════════════════════════════
# 子命令
# ═══════════════════════════════════════════════════════════════


def _cmd_list(store: TaskMapStore) -> str:
    maps = sorted(store.list_all(), key=lambda x: x.id)
    if not maps:
        return "暂无任务。用 `/task new <标题>` 新建，或让我（task_create）建。"
    lines = ["**任务列表**", ""]
    for m in maps:
        q = f" ❓{len(m.open_questions())}" if m.open_questions() else ""
        lines.append(f"- `#{m.id}` **[{_label(m)}]** {m.title}  （节点 {len(m.nodes)} · 待办 {len(m.frontier())}{q}）")
    lines += ["", "看图：`/task view <id>`"]
    return "\n".join(lines)


def _cmd_show(store: TaskMapStore, id_str: str | None) -> str:
    m, err = _pick(store, id_str)
    if err:
        return err
    assert m is not None
    return _fence(render_full(m))


def _cmd_view(store: TaskMapStore, id_str: str | None, flags: set[str], state: Any) -> str:
    m, err = _pick(store, id_str)
    if err:
        return err
    assert m is not None
    if "--tree" in flags:
        mode = "tree"
    elif "--mermaid" in flags:
        mode = "mermaid"
    else:
        mode = "tree" if _frontend(state) == "terminal" else "mermaid"
    return _fence(to_tree(m)) if mode == "tree" else to_markdown(m)


def _cmd_new(store: TaskMapStore, title: str) -> str:
    if not title:
        return "用法：`/task new <标题>`"
    m = store.create(title, "")
    return f"✅ 已创建任务图 `#{m.id}` **{m.title}**（起点 n0 → 目标 n1）。看图：`/task view {m.id}`"


def _cmd_approve(store: TaskMapStore, id_str: str | None) -> str:
    m, err = _pick(store, id_str)
    if err:
        return err
    assert m is not None
    if m.status == MapStatus.COMPLETED:
        return f"任务 `#{m.id}` 已是【已完成】。"
    if m.status == MapStatus.SUPERSEDED:
        return f"⚠ 任务 `#{m.id}` 已作废，无法批准。"
    m.status = MapStatus.COMPLETED
    g = m.goal_node()
    if g is not None and g.status != NodeStatus.DONE:
        g.status = NodeStatus.DONE
        g.log("approved", by="user")
    store.save_map(m)
    return f"✅ 已批准任务 `#{m.id}` → **已完成**。"


def _cmd_reject(store: TaskMapStore, id_str: str | None, reason: str) -> str:
    m, err = _pick(store, id_str)
    if err:
        return err
    assert m is not None
    guard = _status_guard(m, (MapStatus.DELIVERED,), "打回")
    if guard:
        return guard
    m.status = MapStatus.ACTIVE
    note = f"[用户打回] {reason}" if reason else "[用户打回] 未达标，请继续"
    m.questions.append({"q": note, "resolved": False, "ts": time.time(), "by": "user"})
    store.save_map(m)
    tail = f" 原因已记入待办：{reason}" if reason else ""
    return f"↩️ 已打回任务 `#{m.id}` → **进行中**。{tail}"


def _cmd_pause(store: TaskMapStore, id_str: str | None) -> str:
    m, err = _pick(store, id_str)
    if err:
        return err
    assert m is not None
    guard = _status_guard(m, (MapStatus.ACTIVE,), "暂停")
    if guard:
        return guard
    m.status = MapStatus.PAUSED
    store.save_map(m)
    return f"⏳ 已暂停 `#{m.id}`。恢复：`/task resume {m.id}`"


def _cmd_resume(store: TaskMapStore, id_str: str | None) -> str:
    m, err = _pick(store, id_str)
    if err:
        return err
    assert m is not None
    guard = _status_guard(m, (MapStatus.PAUSED,), "恢复")
    if guard:
        return guard
    m.status = MapStatus.ACTIVE
    store.save_map(m)
    return f"▶ 已恢复 `#{m.id}`。"


def _cmd_abort(store: TaskMapStore, id_str: str | None) -> str:
    m, err = _pick(store, id_str)
    if err:
        return err
    assert m is not None
    if m.status == MapStatus.SUPERSEDED:
        return f"任务 `#{m.id}` 已是【已作废】。"
    m.status = MapStatus.SUPERSEDED
    store.save_map(m)
    return f"⛔ 已作废 `#{m.id}`（superseded）。`/task clear` 可清除。"


def _cmd_answer(store: TaskMapStore, rest: str) -> str:
    parts = rest.split(maxsplit=2)
    if len(parts) < 3:
        return "用法：`/task answer <id> <q#> <文本>`"
    mid, qidx, text = parts
    m, err = _pick(store, mid)
    if err:
        return err
    assert m is not None
    try:
        idx = int(qidx)
    except ValueError:
        return f"q# 需为整数：{qidx!r}"
    if not (0 <= idx < len(m.questions)):
        return f"问题下标越界：{idx}（共 {len(m.questions)} 条）"
    q = m.questions[idx]
    q["resolved"] = True
    q["answer"] = text
    q["answered_by"] = "user"
    q["answered_ts"] = time.time()
    store.save_map(m)
    return f"✅ 已回应任务 `#{m.id}` 的待澄清 [{idx}]：{text}"


def _cmd_clear(store: TaskMapStore) -> str:
    term = [m for m in store.list_all() if m.status in TERMINAL_MAP_STATUSES]
    n = store.clear_terminal()
    if n == 0:
        return "没有终态任务需要清除（无已完成/已作废任务）。"
    detail = "、".join(f"#{m.id}" for m in term)
    return f"✅ 已清除 {n} 个终态任务（{detail}）。"


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════


async def execute(state: Any, arg: str) -> tuple[bool, str]:
    raw = (arg or "").strip()
    sub, _, rest = raw.partition(" ")
    sub = sub.lower()
    rest = rest.strip()
    store = TaskMapStore()

    try:
        if sub in ("", "list", "ls"):
            return True, _cmd_list(store)
        if sub in ("help", "-h", "--help"):
            return True, _HELP
        if sub == "show":
            return True, _cmd_show(store, rest or None)
        if sub == "view":
            toks = rest.split()
            flags = {t for t in toks if t.startswith("--")}
            ids = [t for t in toks if not t.startswith("--")]
            return True, _cmd_view(store, ids[0] if ids else None, flags, state)
        if sub == "new":
            return True, _cmd_new(store, rest)
        if sub == "approve":
            return True, _cmd_approve(store, rest or None)
        if sub == "reject":
            mid, _, reason = rest.partition(" ")
            return True, _cmd_reject(store, mid or None, reason.strip())
        if sub == "answer":
            return True, _cmd_answer(store, rest)
        if sub == "pause":
            return True, _cmd_pause(store, rest or None)
        if sub == "resume":
            return True, _cmd_resume(store, rest or None)
        if sub == "abort":
            return True, _cmd_abort(store, rest or None)
        if sub == "clear":
            return True, _cmd_clear(store)
    except Exception as e:  # noqa: BLE001 — 命令不应把异常抛回主循环
        return True, f"❌ `/task {sub}` 失败：{e}"

    return True, f"未知子命令 `{sub}`。\n\n{_HELP}"
