"""Task 工具处理函数 — 任务图(taskmap)

6 个工具: task_create / task_read / task_update / task_edit / task_list / task_clear

- `task_edit` 是「唯一写者」(主 agent)改图的手: 批量 op、整批原子、逐条校验。
- worker **不持有**这些工具; worker 只通过 delta 提议, 由编排器落图。
"""

from __future__ import annotations

import time
from typing import Any, cast

from fp_core.taskmap.graph import GraphOpError, apply_ops
from fp_core.taskmap.models import TERMINAL_MAP_STATUSES, MapStatus
from fp_core.taskmap.render import MAP_LABELS, render_full
from fp_core.taskmap.store import TaskMapStore

# ═══════════════════════════════════════════════════════════════
# OpenAI Function Calling Schema
# ═══════════════════════════════════════════════════════════════

_OP_DESC = (
    "图变更 op 数组(整批原子、逐条校验)。支持的 op: "
    "①{op:'add_node', desc, kind?('step'|'goal'|'side_effect')} 新增状态节点; "
    "②{op:'add_edge', from, to, semantic, label?} 新增转移(from/to 可为同批新节点的 id); "
    "③{op:'set_status', node, status('pending'|'active'|'done'|'failed'|'blocked'|'skipped')}; "
    "④{op:'set_evidence', node, evidence:[...]} 追加证据。"
    "示例(拆图): [{'op':'add_node','desc':'病因已定位'},"
    "{'op':'add_edge','from':'n1','to':'n2','semantic':'requires_decompose'}]"
)

DEF_CREATE: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "task_create",
        "description": "创建一个任务(一张任务图): 默认含起点 n0 与目标 n1。长任务/多步任务用。",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "任务标题(简短)"},
                "goal": {"type": "string", "description": "目标(验收标准的种子; 省略则用 title)"},
            },
            "required": ["title"],
        },
    },
}

DEF_READ: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "task_read",
        "description": "查看任务图的全文(节点/边/待澄清)。",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "任务 ID"}},
            "required": ["id"],
        },
    },
}

DEF_UPDATE: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "task_update",
        "description": "更新任务图的元信息(非图结构): 整体状态 / 标题 / 待澄清问题 / 引用。",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "任务 ID"},
                "status": {
                    "type": "string",
                    "enum": ["active", "blocked", "delivered", "completed", "superseded"],
                    "description": (
                        "整体状态: active=进行中, blocked=卡住待输入, "
                        "delivered=已交付待批准(停手等用户), completed=已完成(用户批准), "
                        "superseded=已作废"
                    ),
                },
                "title": {"type": "string"},
                "add_question": {"type": "string", "description": "追加一条待澄清问题"},
                "resolve_question": {"type": "integer", "description": "标记第 idx 条问题为已解决(从 0 计)"},
                "refs": {"type": "object", "description": "合并进 refs 的键值(如 run_id/artifact 路径)"},
            },
            "required": ["id"],
        },
    },
}

DEF_EDIT: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "task_edit",
        "description": "编辑任务图的**结构**(唯一写者的手): 拆图/加边/改节点状态/加证据。整批原子。",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "任务 ID"},
                "ops": {
                    "type": "array",
                    "description": _OP_DESC,
                    "items": {"type": "object"},
                },
            },
            "required": ["id", "ops"],
        },
    },
}

DEF_LIST: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "task_list",
        "description": "列出所有任务及其状态概览。",
        "parameters": {"type": "object", "properties": {}},
    },
}

DEF_CLEAR: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "task_clear",
        "description": (
            "清除终态任务(已完成 completed 和已作废 superseded)。"
            "注意: 不可撤销、无法指定单个; delivered(已交付待批准)不会被清除。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

ALL_DEFINITIONS: list[dict[str, Any]] = [DEF_CREATE, DEF_READ, DEF_UPDATE, DEF_EDIT, DEF_LIST, DEF_CLEAR]


# ═══════════════════════════════════════════════════════════════
# 处理函数(签名: async def(params: dict) -> str)
# ═══════════════════════════════════════════════════════════════


async def handle_create(params: dict[str, Any]) -> str:
    title = str(params.get("title", "") or "").strip()
    if not title:
        raise ValueError("task_create 需要 title 参数")
    goal = str(params.get("goal", "") or "").strip()
    m = TaskMapStore().create(title, goal)
    return f"✅ 已创建任务图 #{m.id}: {m.title}(起点 n0 → 目标 n1)"


async def handle_read(params: dict[str, Any]) -> str:
    mid = params.get("id")
    if mid is None:
        raise ValueError("task_read 需要 id 参数")
    m = TaskMapStore().get(mid)
    if m is None:
        return f"错误: 未找到任务 #{mid}(可用 task_list 查看)"
    return render_full(m)


async def handle_update(params: dict[str, Any]) -> str:
    mid = params.get("id")
    if mid is None:
        raise ValueError("task_update 需要 id 参数")
    store = TaskMapStore()
    m = store.get(mid)
    if m is None:
        return f"错误: 未找到任务 #{mid}(可用 task_list 查看)"

    changed: list[str] = []

    status = params.get("status")
    if status is not None:
        s = str(status)
        if not MapStatus.is_valid(s):
            raise ValueError(f"非法 status: {s!r}(可选 {[x.value for x in MapStatus]})")
        m.status = MapStatus(s)
        changed.append(f"status={s}")

    title = params.get("title")
    if title:
        m.title = str(title)
        changed.append("title")

    aq = params.get("add_question")
    if aq:
        m.questions.append({"q": str(aq), "resolved": False, "ts": time.time()})
        changed.append("+question")

    rq = params.get("resolve_question")
    if rq is not None:
        try:
            idx = int(rq)
        except (TypeError, ValueError) as e:
            raise ValueError(f"resolve_question 需为整数下标: {rq!r}") from e
        if not (0 <= idx < len(m.questions)):
            raise ValueError(f"问题下标越界: {idx}(共 {len(m.questions)} 条)")
        m.questions[idx]["resolved"] = True
        changed.append(f"resolve#{idx}")

    refs = params.get("refs")
    if isinstance(refs, dict) and refs:
        m.refs.update(cast(dict[str, Any], refs))
        changed.append("refs")

    if not changed:
        return "无变更(未提供任何可更新字段)"

    store.save_map(m)
    return f"✅ 任务 #{m.id} 已更新: {', '.join(changed)}"


async def handle_edit(params: dict[str, Any]) -> str:
    mid = params.get("id")
    ops = params.get("ops")
    if mid is None or ops is None:
        raise ValueError("task_edit 需要 id 和 ops 参数")
    store = TaskMapStore()
    m = store.get(mid)
    if m is None:
        return f"错误: 未找到任务 #{mid}(可用 task_list 查看)"
    try:
        summary = apply_ops(m, ops, by="agent")
    except GraphOpError as e:
        return f"❌ 图变更被拒(整批未落地): {e}"
    store.save_map(m)
    return "✅ 图变更已应用:\n" + "\n".join(f"  {s}" for s in summary)


async def handle_list(params: dict[str, Any]) -> str:
    maps = TaskMapStore().list_all()
    if not maps:
        return "暂无任务"
    lines = ["📋 任务列表:", ""]
    for m in sorted(maps, key=lambda x: x.id):
        label = MAP_LABELS.get(m.status.value, m.status.value)
        q = f" ❓{len(m.open_questions())}" if m.open_questions() else ""
        lines.append(f"  #{m.id} [{label}] {m.title}  (节点{len(m.nodes)} · 待办{len(m.frontier())}{q})")
    return "\n".join(lines)


async def handle_clear(params: dict[str, Any]) -> str:
    store = TaskMapStore()
    term = [m for m in store.list_all() if m.status in TERMINAL_MAP_STATUSES]
    n = store.clear_terminal()
    if n == 0:
        return "没有终态任务需要清除(无已完成/已作废任务)"
    detail = "、".join(f"#{m.id} {m.title}" for m in term)
    return f"✅ 已清除 {n} 个终态任务({detail}), 剩余 {len(store.list_all())} 个"
