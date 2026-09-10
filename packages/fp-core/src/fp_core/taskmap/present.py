"""任务图呈现 — 面向人的多前端渲染

同一张图, 多种呈现:

- `to_mermaid(m)`  : Mermaid 图源(webui / acp / 剪贴板)
- `to_tree(m)`     : 终端树形文本(终端画不了 mermaid, 退化为树 + 未连接节点)
- `to_markdown(m)` : md 内嵌 mermaid(webui/acp 的规范产物)

原则:
- **确定性**: 图源不含时间戳等易变字段, 同图同画(便于核对与 diff)。
- **保真**: 显示真实结构与状态, 不做美化摘要; 异常显式标出。
- **同源**: 三种呈现读同一模型, 保真度随前端而异。

依赖: 仅 `models`/`render`(同包), 无第三方依赖。
"""

from __future__ import annotations

import time

from .models import NodeKind, NodeStatus, TaskMap
from .render import MAP_LABELS

__all__ = ["to_mermaid", "to_tree", "to_markdown"]

# ═══════════════════════════════════════════════════════════════
# Mermaid 常量
# ═══════════════════════════════════════════════════════════════

#: 形状随节点种类: 起点=圆, 目标=胶囊, 中间=方框, 副作用=六边形
_SHAPE: dict[NodeKind, tuple[str, str]] = {
    NodeKind.START: ("((", "))"),
    NodeKind.GOAL: ("([", "])"),
    NodeKind.STEP: ("[", "]"),
    NodeKind.SIDE_EFFECT: ("{{", "}}"),
}

_CLASSDEFS = (
    "  classDef done fill:#d3f9d8,stroke:#2b8a3e,color:#1b4332\n"
    "  classDef active fill:#d0ebff,stroke:#1971c2,color:#0b4a8f\n"
    "  classDef pending fill:#f1f3f5,stroke:#adb5bd,color:#343a40\n"
    "  classDef failed fill:#ffe3e3,stroke:#c92a2a,color:#7a1f1f\n"
    "  classDef blocked fill:#fff3bf,stroke:#e67700,color:#7a4a00\n"
    "  classDef skipped fill:#f8f9fa,stroke:#ced4da,color:#868e96\n"
    "  classDef anomaly stroke:#c92a2a,stroke-width:3px,stroke-dasharray:5 3"
)

_STATUS_ICON: dict[str, str] = {
    "pending": "⬜",
    "active": "▶",
    "done": "✅",
    "failed": "❌",
    "blocked": "❗",
    "skipped": "⏭",
}


# ═══════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════


def _nid_key(nid: str) -> tuple[int, str]:
    """n0/n1/... 按数字排序; 其余排最后(与 render.py 同序)。"""
    if nid.startswith("n") and nid[1:].isdigit():
        return (int(nid[1:]), "")
    return (10**9, nid)


def _esc(text: str, limit: int = 28) -> str:
    """转义 mermaid 标签: 折叠空白/截断/转义引号#竖线。"""
    s = " ".join(str(text).split())
    if len(s) > limit:
        s = s[: max(1, limit - 1)] + "…"
    return s.replace('"', "&quot;").replace("#", "&num;").replace("|", "&#124;")


def _adj(m: TaskMap) -> dict[str, list[str]]:
    adj: dict[str, list[str]] = {}
    for e in m.edges:
        if e.src in m.nodes and e.dst in m.nodes:
            adj.setdefault(e.src, []).append(e.dst)
    return adj


def _reachable_from_start(m: TaskMap) -> set[str]:
    adj = _adj(m)
    seen: set[str] = set()
    stack = ["n0"] if "n0" in m.nodes else []
    while stack:
        x = stack.pop()
        if x in seen:
            continue
        seen.add(x)
        stack.extend(adj.get(x, []))
    return seen


def _cycle_nodes(m: TaskMap) -> set[str]:
    """处于环中的节点(DFS 三色)。"""
    adj = _adj(m)
    color: dict[str, int] = {}
    bad: set[str] = set()

    def dfs(u: str, path: set[str]) -> None:
        color[u] = 1
        path.add(u)
        for v in adj.get(u, []):
            if color.get(v, 0) == 1:
                bad.update(path)
            elif color.get(v, 0) == 0:
                dfs(v, path)
        path.discard(u)
        color[u] = 2

    for nid in m.nodes:
        if color.get(nid, 0) == 0:
            dfs(nid, set())
    return bad


def _anomalies(m: TaskMap, now: float | None = None) -> dict[str, str]:
    """返回 {nid: 原因} —— 漂移/失控信号, 是给人看图的重点。"""
    now = time.time() if now is None else now
    out: dict[str, str] = {}
    if "n1" in m.nodes and "n1" not in _reachable_from_start(m):
        out["n1"] = "目标不可达"
    for nid in _cycle_nodes(m):
        out.setdefault(nid, "处于环中")
    for nid, n in m.nodes.items():
        if n.status == NodeStatus.ACTIVE:
            if n.lease_until and n.lease_until < now:
                out[nid] = "锁已过期"
            elif not n.owner:
                out.setdefault(nid, "进行中但无 owner")
    return out


# ═══════════════════════════════════════════════════════════════
# Mermaid
# ═══════════════════════════════════════════════════════════════


def to_mermaid(m: TaskMap, *, direction: str = "LR", now: float | None = None) -> str:
    """任务图 → Mermaid 图源(确定性)。

    节点 id 消毒为 m<idx>(真实 id 保留在标签里); 形状随种类, 颜色随状态;
    异常节点(side_effect/环/陈旧锁/目标不可达)附加 anomaly 类。
    """
    ids = sorted(m.nodes, key=_nid_key)
    if not ids:
        return f"flowchart {direction}\n  empty[(空图)]"

    alias = {nid: f"m{i}" for i, nid in enumerate(ids)}
    lines: list[str] = [f"flowchart {direction}", _CLASSDEFS]

    for nid in ids:
        n = m.nodes[nid]
        open_, close_ = _SHAPE.get(n.kind, ("[", "]"))
        label = _esc(f"{n.id} {n.desc}")
        lines.append(f'  {alias[nid]}{open_}"{label}"{close_}')

    for e in m.edges:
        if e.src not in alias or e.dst not in alias:
            continue
        sem = _esc(e.semantic, limit=40)
        lab = f"{sem} · {_esc(e.label)}" if e.label else sem
        lines.append(f"  {alias[e.src]} -->|{lab}| {alias[e.dst]}")

    for nid in ids:
        lines.append(f"  class {alias[nid]} {m.nodes[nid].status.value}")
    for nid in _anomalies(m, now):
        lines.append(f"  class {alias[nid]} anomaly")

    return "\n".join(lines)


def to_markdown(m: TaskMap, *, direction: str = "LR", now: float | None = None) -> str:
    """md 内嵌 mermaid(webui/acp 用): 标题 + 目标 + 图 + 异常 + 图例。"""
    label = MAP_LABELS.get(m.status.value, m.status.value)
    lines: list[str] = [f"### 任务 #{m.id} · {m.title}  `[{label}]`", ""]
    if m.goal:
        lines.append(f"**目标**: {m.goal}")
        lines.append("")
    lines.append("```mermaid")
    lines.append(to_mermaid(m, direction=direction, now=now))
    lines.append("```")

    an = _anomalies(m, now)
    if an:
        items = "; ".join(f"`{nid}` {why}" for nid, why in sorted(an.items(), key=lambda kv: _nid_key(kv[0])))
        lines.append("")
        lines.append(f"**⚠ 异常**: {items}")

    lines.append("")
    lines.append(
        "**图例**: ✅完成 ▶进行 ⬜待办 ❌失败 ❗阻塞 ⏭跳过 · 形状: 圆=起点 胶囊=目标 方=步骤 六边形=副作用支线"
    )
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 终端树
# ═══════════════════════════════════════════════════════════════


def to_tree(m: TaskMap, *, now: float | None = None) -> str:
    """任务图 → 终端树形文本。

    图可能不是树(多入边/环), 故: 从 n0 深度优先展开, 已展开节点显示为
    `↩ (已展开)`; 起点不可达的节点单列; 异常单列。
    """
    label = MAP_LABELS.get(m.status.value, m.status.value)
    lines: list[str] = [f"#{m.id} {m.title}  [{label}]"]
    if m.goal:
        lines.append(f"目标: {m.goal}")
    lines.append("")

    out: dict[str, list] = {}
    for e in m.edges:
        if e.src in m.nodes and e.dst in m.nodes:
            out.setdefault(e.src, []).append(e)

    def node_label(nid: str) -> str:
        n = m.nodes[nid]
        icon = _STATUS_ICON.get(n.status.value, "?")
        kind = " [副作用]" if n.kind == NodeKind.SIDE_EFFECT else ""
        return f"{icon} {nid}{kind} {n.desc}"

    def edge_tag(e) -> str:
        return f"  [{e.semantic}{(' · ' + e.label) if e.label else ''}]"

    seen: set[str] = set()

    def walk(nid: str, prefix: str, connector: str, tag: str) -> None:
        lines.append(f"{prefix}{connector}{node_label(nid)}{tag}")
        kids = out.get(nid, [])
        if connector == "├─ ":
            cp = prefix + "│  "
        elif connector == "└─ ":
            cp = prefix + "   "
        else:  # 根节点, 无连接符
            cp = prefix
        for i, e in enumerate(kids):
            last = i == len(kids) - 1
            conn = "└─ " if last else "├─ "
            if e.dst in seen:
                lines.append(f"{cp}{conn}↩ {e.dst} (已展开){edge_tag(e)}")
            else:
                seen.add(e.dst)
                walk(e.dst, cp, conn, edge_tag(e))

    if "n0" in m.nodes:
        seen.add("n0")
        walk("n0", "", "", "")
    else:
        lines.append("(无起点节点 n0)")

    rest = [nid for nid in sorted(m.nodes, key=_nid_key) if nid not in seen]
    if rest:
        lines.append("")
        lines.append("未连接节点:")
        for nid in rest:
            lines.append(f"  {node_label(nid)}")

    an = _anomalies(m, now)
    if an:
        lines.append("")
        lines.append("⚠ 异常:")
        for nid, why in sorted(an.items(), key=lambda kv: _nid_key(kv[0])):
            lines.append(f"  {nid}: {why}")

    return "\n".join(lines)
