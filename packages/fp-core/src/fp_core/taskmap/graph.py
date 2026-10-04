"""任务图变更 op — 纯逻辑(不碰 IO)

被两处共用:
- `task_edit` 工具(主 agent 作为唯一写者的「手」)
- worker delta 的应用(编排器落图)

两条铁律:
1. **原子**: 任一 op 非法 → 整批拒绝, 图分毫不动(先在深拷贝上走完整批, 通过才写回)。
2. **局部 id 可用**: 同一批内 add_node 生成的新节点 id, 可被后续 add_edge 引用。
"""

from __future__ import annotations

import copy
from typing import Any, cast

from .models import Edge, Node, NodeKind, NodeStatus, TaskMap

#: 允许的 op 词表(小而非穷尽; 删除类 op 刻意不提供——删节点风险高, 由人/主 agent 谨慎处理)
VALID_OPS = frozenset({"add_node", "add_edge", "set_status", "set_evidence"})


class GraphOpError(ValueError):
    """op 非法(校验失败)"""


def apply_ops(m: TaskMap, ops: Any, by: str = "agent") -> list[str]:
    """校验并应用一批图 op。

    原子语义: 先在 `copy.deepcopy(m)` 上逐条校验+模拟, 全部通过才写回原对象;
    任一条失败 → 抛 GraphOpError, `m` 保持不变。

    Returns:
        人类可读的变更摘要列表。

    Raises:
        GraphOpError: ops 为空/非列表, 或任一 op 非法。
    """
    # ops 来自 LLM 生成的 JSON(不可信), 故签名用 Any、入口做 isinstance 校验,
    # 通过后 cast 切断 Unknown 级联(strict 模式)。
    if not isinstance(ops, list) or not ops:
        raise GraphOpError("ops 不能为空(需为非空数组)")
    ops_list = cast(list[Any], ops)

    staged = copy.deepcopy(m)
    summary: list[str] = []

    for i, op_raw in enumerate(ops_list):
        if not isinstance(op_raw, dict):
            raise GraphOpError(f"op#{i} 不是对象")
        op = cast(dict[str, Any], op_raw)
        kind = op.get("op")
        if kind not in VALID_OPS:
            raise GraphOpError(f"op#{i} 未知操作: {kind!r}(可选 {sorted(VALID_OPS)})")

        if kind == "add_node":
            desc = str(op.get("desc", "")).strip()
            if not desc:
                raise GraphOpError(f"op#{i} add_node 缺 desc")
            nk = str(op.get("kind", NodeKind.STEP.value))
            if nk not in {k.value for k in NodeKind}:
                raise GraphOpError(f"op#{i} 非法 kind: {nk!r}(可选 {[k.value for k in NodeKind]})")
            nid = staged.new_node_id()
            node = Node(id=nid, desc=desc, kind=NodeKind(nk))
            node.log("added", by=by)
            staged.nodes[nid] = node
            summary.append(f"+节点 {nid} ({nk}) {desc}")

        elif kind == "add_edge":
            src = str(op.get("from", "")).strip()
            dst = str(op.get("to", "")).strip()
            if src not in staged.nodes:
                raise GraphOpError(f"op#{i} add_edge 起点不存在: {src!r}")
            if dst not in staged.nodes:
                raise GraphOpError(f"op#{i} add_edge 终点不存在: {dst!r}")
            sem = str(op.get("semantic", "")).strip()
            if not sem:
                raise GraphOpError(f"op#{i} add_edge 缺 semantic")
            edge = Edge(src=src, dst=dst, semantic=sem, label=str(op.get("label", "")))
            edge.log("added", by=by)
            staged.edges.append(edge)
            summary.append(f"+边 {src} -[{sem}]-> {dst}")

        elif kind == "set_status":
            nid = str(op.get("node", "")).strip()
            node = staged.nodes.get(nid)
            if node is None:
                raise GraphOpError(f"op#{i} set_status 节点不存在: {nid!r}")
            st = str(op.get("status", "")).strip()
            if not NodeStatus.is_valid(st):
                raise GraphOpError(f"op#{i} 非法 status: {st!r}(可选 {[s.value for s in NodeStatus]})")
            node.status = NodeStatus(st)
            node.log(f"status={st}", by=by)
            summary.append(f"节点 {nid} → {st}")

        elif kind == "set_evidence":
            nid = str(op.get("node", "")).strip()
            node = staged.nodes.get(nid)
            if node is None:
                raise GraphOpError(f"op#{i} set_evidence 节点不存在: {nid!r}")
            ev_raw: Any = op.get("evidence")
            if not isinstance(ev_raw, list):
                raise GraphOpError(f"op#{i} set_evidence 的 evidence 必须是数组")
            ev = cast(list[Any], ev_raw)
            node.evidence.extend(str(e) for e in ev)
            node.log("evidence+", by=by)
            summary.append(f"节点 {nid} +证据×{len(ev)}")

    # 全部通过 → 写回原对象(保持 id/created 等元数据不动)
    m.nodes = staged.nodes
    m.edges = staged.edges
    m.next_nid = staged.next_nid
    m.touch()
    return summary
