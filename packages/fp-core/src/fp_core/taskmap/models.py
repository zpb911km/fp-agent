"""任务图(taskmap)数据模型

从「线性清单」升级为「有向图」:

- 一个 TaskMap = 一个任务 = 一张图(起点节点 → 目标节点, 中间节点由「拆图」生长)。
- 节点 = 一个**须成立的状态**(非动作); 带状态与锁(owner/dispatch_id/lease_until)。
- 边 = 一次转移, 携带**结果语义**(为何走到这): complete / exhausted / side_effect /
  requires_decompose / blocked / partial(其余语义自由)。
- 图可动态生长: 拆图长出新层; 副作用自长支线(side_effect 指向新建修复节点)。

协作模型: 人与所有 agent 共读同一张图; 「单写者」(编排器)是唯一落图者,
worker 只通过结构化 delta **提议**变更。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: 单文件 JSON 的 schema 版本(旧 {tasks:[...]} 视为 v1, 惰性迁移)。
SCHEMA_VERSION = 2


# ═══════════════════════════════════════════════════════════════
# 枚举
# ═══════════════════════════════════════════════════════════════


class NodeKind(StrEnum):
    """节点种类"""

    START = "start"  # 现状
    GOAL = "goal"  # 目标
    STEP = "step"  # 中间步骤
    SIDE_EFFECT = "side_effect"  # 副作用支线(如「引入 bug 后新建的修复节点」)


class NodeStatus(StrEnum):
    """节点状态(该状态是否已成立)"""

    PENDING = "pending"  # 未达
    ACTIVE = "active"  # 进行中(通常被某个 worker 锁定)
    DONE = "done"  # 已达成
    FAILED = "failed"  # 尝试后失败
    BLOCKED = "blocked"  # 卡住, 需外部输入(上报)
    SKIPPED = "skipped"  # 被策略跳过

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in {s.value for s in cls}


class MapStatus(StrEnum):
    """任务图(任务)的整体状态。承接旧 task 状态机语义。"""

    ACTIVE = "active"  # 进行中(旧 pending/in_progress 均归此)
    PAUSED = "paused"  # 用户暂停(安全阀: 停手但不作废)
    BLOCKED = "blocked"  # 卡住待外部输入
    DELIVERED = "delivered"  # 已交付待批准(停手等用户)
    COMPLETED = "completed"  # 用户已批准
    SUPERSEDED = "superseded"  # 被推翻/作废

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in {s.value for s in cls}


#: 终态 map(可被 task_clear 清除)。注意 delivered 不是终态。
TERMINAL_MAP_STATUSES = frozenset({MapStatus.COMPLETED, MapStatus.SUPERSEDED})

#: 节点终态(不再参与进一步调度)。
TERMINAL_NODE_STATUSES = frozenset({NodeStatus.DONE, NodeStatus.FAILED, NodeStatus.SKIPPED})


class EdgeSemantic(StrEnum):
    """保留边语义。

    框架仅按这些词识别关键状态(如 blocked 需显著化、side_effect 需汇回);
    其余语义由使用者自由定义, 框架不解释。
    """

    REQUIRES_DECOMPOSE = "requires_decompose"  # 该转移尚不能直达, 需拆图
    COMPLETE = "complete"  # 完美实现 → 通往下一状态
    EXHAUSTED = "exhausted"  # 尝试 N 次失败 → 转向其他假设
    SIDE_EFFECT = "side_effect"  # 引入副作用 → 指向新建修复节点
    BLOCKED = "blocked"  # 需外部输入
    PARTIAL = "partial"  # 部分达成


RESERVED_SEMANTICS = frozenset(s.value for s in EdgeSemantic)


def _now() -> float:
    return time.time()


# ═══════════════════════════════════════════════════════════════
# 节点 / 边
# ═══════════════════════════════════════════════════════════════


@dataclass
class Node:
    """图中的一个状态节点"""

    id: str
    desc: str
    kind: NodeKind = NodeKind.STEP
    status: NodeStatus = NodeStatus.PENDING
    # 锁(仅单写者维护): 谁在做、哪个 attempt、租约到期
    owner: str | None = None
    dispatch_id: str | None = None
    lease_until: float | None = None
    # 证据绑定: 把「宣称达成」钉到「凭据」(路径/命令输出/结果引用)
    evidence: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    def log(self, action: str, by: str = "agent", note: str = "") -> None:
        self.history.append({"ts": _now(), "by": by, "action": action, "note": note})

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "desc": self.desc,
            "kind": self.kind.value,
            "status": self.status.value,
            "owner": self.owner,
            "dispatch_id": self.dispatch_id,
            "lease_until": self.lease_until,
            "evidence": list(self.evidence),
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Node:
        return cls(
            id=str(data["id"]),
            desc=str(data.get("desc", "")),
            kind=NodeKind(data.get("kind", NodeKind.STEP.value)),
            status=NodeStatus(data.get("status", NodeStatus.PENDING.value)),
            owner=data.get("owner"),
            dispatch_id=data.get("dispatch_id"),
            lease_until=data.get("lease_until"),
            evidence=[str(e) for e in (data.get("evidence") or [])],
            history=list(data.get("history") or []),
        )


@dataclass
class Edge:
    """一条带结果语义的转移。

    内部字段用 src/dst(避开 Python 关键字 from); 序列化为 JSON 的 "from"/"to"。
    """

    src: str
    dst: str
    semantic: str
    label: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    def log(self, action: str, by: str = "agent", note: str = "") -> None:
        self.history.append({"ts": _now(), "by": by, "action": action, "note": note})

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": self.src,
            "to": self.dst,
            "semantic": self.semantic,
            "label": self.label,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Edge:
        return cls(
            src=str(data["from"]),
            dst=str(data["to"]),
            semantic=str(data.get("semantic", "")),
            label=str(data.get("label", "")),
            history=list(data.get("history") or []),
        )


# ═══════════════════════════════════════════════════════════════
# 任务图
# ═══════════════════════════════════════════════════════════════

#: 旧 task 状态 → 新 map 状态
_LEGACY_STATUS = {
    "pending": MapStatus.ACTIVE.value,
    "in_progress": MapStatus.ACTIVE.value,
    "delivered": MapStatus.DELIVERED.value,
    "completed": MapStatus.COMPLETED.value,
    "superseded": MapStatus.SUPERSEDED.value,
}

START_NODE_ID = "n0"
GOAL_NODE_ID = "n1"

#: 旧任务状态 → 目标节点状态(避免"已完成任务却显示有待办")
_LEGACY_GOAL_NODE_STATUS = {
    MapStatus.COMPLETED: NodeStatus.DONE,
    MapStatus.DELIVERED: NodeStatus.DONE,
    MapStatus.SUPERSEDED: NodeStatus.SKIPPED,
}


@dataclass
class TaskMap:
    """一张任务图 = 一个任务"""

    id: int
    title: str
    goal: str = ""
    status: MapStatus = MapStatus.ACTIVE
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)
    refs: dict[str, Any] = field(default_factory=dict)
    created: float = field(default_factory=_now)
    updated: float = field(default_factory=_now)
    #: 节点 id 单调计数器(新节点 = n<next_nid>)。只增, 保证 id 稳定不复用。
    next_nid: int = 0

    # ── 查询 ───────────────────────────────────────

    def node(self, nid: str) -> Node | None:
        return self.nodes.get(nid)

    def start_node(self) -> Node | None:
        return self.nodes.get(START_NODE_ID)

    def goal_node(self) -> Node | None:
        return self.nodes.get(GOAL_NODE_ID)

    def out_edges(self, nid: str) -> list[Edge]:
        return [e for e in self.edges if e.src == nid]

    def in_edges(self, nid: str) -> list[Edge]:
        return [e for e in self.edges if e.dst == nid]

    def edge_to(self, src: str, dst: str) -> Edge | None:
        for e in self.edges:
            if e.src == src and e.dst == dst:
                return e
        return None

    def open_questions(self) -> list[dict[str, Any]]:
        return [q for q in self.questions if not q.get("resolved")]

    def active_nodes(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.status == NodeStatus.ACTIVE]

    def frontier(self) -> list[Node]:
        """待处理节点: 非终态、且不是起点。"""
        return [n for n in self.nodes.values() if n.status not in TERMINAL_NODE_STATUSES and n.kind != NodeKind.START]

    def touch(self) -> None:
        self.updated = _now()

    # ── id 分配 ────────────────────────────────────

    def new_node_id(self) -> str:
        nid = f"n{self.next_nid}"
        self.next_nid += 1
        return nid

    # ── 序列化 ─────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "status": self.status.value,
            "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
            "edges": [e.to_dict() for e in self.edges],
            "questions": list(self.questions),
            "refs": dict(self.refs),
            "created": self.created,
            "updated": self.updated,
            "next_nid": self.next_nid,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskMap:
        raw_nodes = data.get("nodes") or {}
        nodes: dict[str, Node] = {}
        if isinstance(raw_nodes, dict):
            for nid, nd in raw_nodes.items():
                nodes[str(nid)] = Node.from_dict(nd)
        nn = data.get("next_nid")
        if nn is None:
            nums = [int(nid[1:]) for nid in nodes if nid.startswith("n") and nid[1:].isdigit()]
            nn = (max(nums) + 1) if nums else len(nodes)
        return cls(
            id=int(data["id"]),
            title=str(data.get("title", "")),
            goal=str(data.get("goal", "")),
            status=MapStatus(data.get("status", MapStatus.ACTIVE.value)),
            nodes=nodes,
            edges=[Edge.from_dict(e) for e in (data.get("edges") or [])],
            questions=list(data.get("questions") or []),
            refs=dict(data.get("refs") or {}),
            created=float(data.get("created", _now())),
            updated=float(data.get("updated", _now())),
            next_nid=int(nn),
        )

    # ── 构造 ───────────────────────────────────────

    @classmethod
    def create(cls, mid: int, title: str, goal: str = "") -> TaskMap:
        """新建任务图: 起点节点 + 目标节点 + 一条 requires_decompose 边。"""
        m = cls(id=mid, title=title, goal=goal or title)
        start = Node(
            id=START_NODE_ID,
            desc="(起点)",
            kind=NodeKind.START,
            status=NodeStatus.DONE,
        )
        start.log("created", by="agent")
        goal_node = Node(id=GOAL_NODE_ID, desc=m.goal, kind=NodeKind.GOAL)
        goal_node.log("created", by="agent")
        m.nodes = {START_NODE_ID: start, GOAL_NODE_ID: goal_node}
        m.edges = [Edge(src=START_NODE_ID, dst=GOAL_NODE_ID, semantic=EdgeSemantic.REQUIRES_DECOMPOSE.value)]
        m.next_nid = 2
        return m

    @classmethod
    def from_legacy(cls, data: dict[str, Any]) -> TaskMap:
        """旧 {id, subject, [description], status} → 任务图。"""
        mid = int(data["id"])
        subject = str(data.get("subject", "") or "").strip()
        desc = str(data.get("description", "") or "").strip()
        goal = desc or subject or "(旧任务)"
        status = MapStatus(_LEGACY_STATUS.get(str(data.get("status", "")), MapStatus.ACTIVE.value))
        m = cls.create(mid, subject or goal, goal)
        m.status = status
        # 目标节点随旧任务状态对齐: 已完成/已交付 → 目标已达成; 作废 → 跳过
        goal_node = m.nodes[GOAL_NODE_ID]
        goal_node.status = _LEGACY_GOAL_NODE_STATUS.get(status, NodeStatus.PENDING)
        goal_node.log("migrated", by="migration", note=f"status={goal_node.status.value}")
        m.nodes[START_NODE_ID].log("migrated", by="migration")
        return m
