"""任务图渲染 — 文本化(给人读, 也给 LLM 读)

- `render_full(m)`      : `task_read` 的全文
- `render_subgraph(m, center, radius)`: 注入 worker 上下文的子图(含目标节点 + 邻域 + 边语义图例)
- `summary(maps)`       : `[task]` 紧凑提醒

LLM 读文本图比读 JSON 稳, 故不返回 JSON。
"""

from __future__ import annotations

from .models import MapStatus, Node, TaskMap

STATUS_LABELS: dict[str, str] = {
    "pending": "待办",
    "active": "进行中",
    "done": "已完成",
    "failed": "失败",
    "blocked": "阻塞",
    "skipped": "跳过",
}

STATUS_ICONS: dict[str, str] = {
    "pending": "⬜",
    "active": "▶",
    "done": "✅",
    "failed": "❌",
    "blocked": "❗",
    "skipped": "⏭",
}

MAP_LABELS: dict[str, str] = {
    "active": "进行中",
    "blocked": "阻塞",
    "delivered": "交付待批",
    "completed": "已完成",
    "superseded": "已作废",
}

EDGE_LEGEND = (
    "边语义: complete=完美实现 / exhausted=尝试N次失败 / side_effect=引入副作用(指向新建修复节点) / "
    "blocked=需外部输入 / partial=部分达成 / requires_decompose=需拆图(其余语义自由)"
)


def _node_line(n: Node, mark: bool = False) -> str:
    icon = STATUS_ICONS.get(n.status.value, "?")
    label = STATUS_LABELS.get(n.status.value, n.status.value)
    extra: list[str] = []
    if n.owner:
        extra.append(f"owner={n.owner}")
    if n.evidence:
        extra.append(f"证据×{len(n.evidence)}")
    tail = ("  " + " ".join(extra)) if extra else ""
    cur = "  ← 当前" if mark else ""
    return f"  {n.id} ({n.kind.value}) {icon}{label} {n.desc}{tail}{cur}"


def _edge_line(src: str, dst: str, semantic: str, label: str = "") -> str:
    mid = f"-[{semantic}]->"
    tail = f"  ({label})" if label else ""
    return f"  {src} {mid} {dst}{tail}"


def render_full(m: TaskMap) -> str:
    """任务图全文(供 task_read)"""
    lines: list[str] = []
    lines.append(f"# {m.id}  {m.title}  [{MAP_LABELS.get(m.status.value, m.status.value)}]")
    if m.goal:
        lines.append(f"目标: {m.goal}")
    lines.append("")
    lines.append("节点:")
    for nid in sorted(m.nodes, key=_nid_key):
        lines.append(_node_line(m.nodes[nid]))
    lines.append("")
    lines.append("边:")
    if m.edges:
        for e in m.edges:
            lines.append(_edge_line(e.src, e.dst, e.semantic, e.label))
    else:
        lines.append("  (无)")
    qs = m.open_questions()
    if qs:
        lines.append("")
        lines.append(f"待澄清({len(qs)}):")
        for q in qs:
            lines.append(f"  ? {q.get('q', '')}")
    return "\n".join(lines)


def render_subgraph(m: TaskMap, center: str, radius: int = 2) -> str:
    """以 center 为中心、radius 跳内的子图(注入 worker 上下文用)。

    始终包含目标节点(防 worker 局限局部忘了终点)。
    """
    keep: set[str] = {center} if center in m.nodes else set()
    if "n1" in m.nodes:
        keep.add("n1")  # 目标节点恒在

    # BFS(双向)扩展 radius 跳
    frontier = set(keep)
    for _ in range(max(0, radius)):
        nxt: set[str] = set()
        for nid in frontier:
            for e in m.edges:
                if e.src == nid and e.dst in m.nodes:
                    nxt.add(e.dst)
                if e.dst == nid and e.src in m.nodes:
                    nxt.add(e.src)
        nxt -= keep
        keep |= nxt
        frontier = nxt

    lines: list[str] = []
    lines.append(f"[子图] 任务#{m.id} {m.title} | 目标: {m.goal}")
    if center in m.nodes:
        lines.append(f"当前节点: {_node_line(m.nodes[center]).strip()}")
    lines.append(f"邻域(半径{radius}):")
    for nid in sorted(keep, key=_nid_key):
        lines.append(_node_line(m.nodes[nid], mark=(nid == center)))
    lines.append("边:")
    shown = [e for e in m.edges if e.src in keep and e.dst in keep]
    if shown:
        for e in shown:
            lines.append(_edge_line(e.src, e.dst, e.semantic, e.label))
    else:
        lines.append("  (无)")
    lines.append(EDGE_LEGEND)
    return "\n".join(lines)


def summary(maps: list[TaskMap], limit: int = 3) -> str | None:
    """紧凑提醒。仅非终态 map; 无则返回 None。"""
    live = [m for m in maps if m.status not in (MapStatus.COMPLETED, MapStatus.SUPERSEDED)]
    if not live:
        return None
    live.sort(key=lambda x: x.id)
    parts: list[str] = []
    for m in live[:limit]:
        icon = "⏸" if m.status == MapStatus.DELIVERED else "▶"
        active = m.active_nodes()
        act = f" 进行:{active[0].id}({active[0].desc})" if active else ""
        todo = len(m.frontier())
        q = len(m.open_questions())
        qs = f" ❓{q}" if q else ""
        parts.append(f"{icon}#{m.id} {m.title}{act} · 待办:{todo}{qs}")
    if len(live) > limit:
        parts.append(f"…(+{len(live) - limit})")
    return " | ".join(parts)


def _nid_key(nid: str) -> tuple[int, str]:
    return (int(nid[1:]), "") if nid.startswith("n") and nid[1:].isdigit() else (10**9, nid)
