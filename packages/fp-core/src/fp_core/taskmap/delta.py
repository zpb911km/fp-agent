"""worker ↔ 任务图 协议: delta 解析 + 校验 + 应用

原则: worker 交回的不是「写入」, 是「局部观察 + 提议」; 编排器是唯一落图者。

- `extract_delta(stdout)`       : 从 worker 输出里取出最后一个 [taskmap-delta] 块
- `apply_delta(m, delta, ...)`  : 8 条校验 + 应用(outcome 一律落, propose 全有或全无)
- `DELTA_INSTRUCTION`           : 注入 worker 提示, 告诉它如何回报

8 条校验(见记忆 taskmap_worker_protocol):
  1 节点存在且归本 attempt(dispatch_id/owner 匹配)
  2 outcome 是已有出边语义或本次新建
  3 新边端点可解析
  4 局部 id → 全局 id 重写
  5 身份去重(desc 完全相同则复用已有节点)
  6 作用域: 只能碰自己节点 + 本次新建节点
  7 环检测(仅告警)
  8 目标可达性(不可达 → 告警)
"""

from __future__ import annotations

import json
import time
from typing import Any

from .graph import GraphOpError, apply_ops
from .models import NodeKind, NodeStatus, TaskMap

DELTA_START = "[taskmap-delta]"
DELTA_END = "[/taskmap-delta]"

#: 注入 worker 提示的回报约定(由编排器追加到 worker context)
DELTA_INSTRUCTION = (
    "【任务图回报(可选)】\n"
    "完成/受阻后, 若你改变了任务进度, 在**最后**用一个块回报(其余正文照常):\n"
    f"{DELTA_START}\n"
    '{"dispatch_id":"<原样回传>","node":"<你的节点id>","status":"done|failed|blocked|partial",'
    '"outcome":"complete|exhausted|side_effect|blocked|partial|<自由语义>",'
    '"evidence":["命令/文件/结果引用"],'
    '"propose":{"nodes":[{"lid":"x1","desc":"新节点"}],'
    '"edges":[{"from":"<节点id>","to":"x1","semantic":"complete"}]},'
    '"suggest_next":["x1"],"questions":["需澄清的问题"]}\n'
    f"{DELTA_END}\n"
    "规则: 只能碰你自己的节点与本次新建节点; 无变化可不回报; 块必须在末尾、仅一个。"
)


def delta_instruction(node_id: str, dispatch_id: str) -> str:
    """生成注入某 worker 的回报约定(带它的 node_id / dispatch_id)。"""
    example = (
        '{"dispatch_id":"' + dispatch_id + '","node":"' + node_id + '",'
        '"status":"done|failed|blocked|partial",'
        '"outcome":"complete|exhausted|side_effect|blocked|partial",'
        '"evidence":["命令/文件/结果引用"],'
        '"propose":{"nodes":[{"lid":"x1","desc":"新节点"}],'
        '"edges":[{"from":"' + node_id + '","to":"x1","semantic":"complete"}]},'
        '"suggest_next":["x1"],"questions":["需澄清的问题"]}'
    )
    return (
        f"【任务图回报 · 你的节点 {node_id}】\n"
        "若你推进或受阻了该节点, 请在**最后**用一个块回报(正文照常):\n"
        f"{DELTA_START}\n{example}\n{DELTA_END}\n"
        f"规则: dispatch_id/node 照抄; 只能碰你的节点({node_id})与本次新建节点; 块须在末尾、仅一个。"
    )


#: delta.status → 节点状态
_STATUS_MAP = {
    "done": NodeStatus.DONE,
    "failed": NodeStatus.FAILED,
    "blocked": NodeStatus.BLOCKED,
    "partial": NodeStatus.ACTIVE,
}
_VALID_STATUS = set(_STATUS_MAP)


def extract_delta(stdout: str) -> tuple[dict[str, Any] | None, str]:
    """从 worker stdout 取最后一个 [taskmap-delta] 块并解析。

    Returns:
        (delta | None, note)。note 为空表示无错(也无块时为 "无 delta 块")。
    """
    if not stdout or DELTA_START not in stdout:
        return None, "无 delta 块"
    i = stdout.rfind(DELTA_START)
    j = stdout.find(DELTA_END, i + len(DELTA_START))
    if j < 0:
        return None, "delta 块缺少结束哨兵"
    raw = stdout[i + len(DELTA_START) : j].strip()
    # 容忍 ```json 围栏
    raw = raw.strip("`")
    if raw.lower().startswith("json"):
        raw = raw[4:].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"delta JSON 解析失败: {e}"
    if not isinstance(parsed, dict):
        return None, "delta 顶层不是对象"
    return parsed, ""


def _resolve(name: str, lid_map: dict[str, str], existing: set[str]) -> str | None:
    if name in lid_map:
        return lid_map[name]
    if name in existing:
        return name
    return None


def _has_cycle(m: TaskMap) -> bool:
    color: dict[str, int] = {}

    def dfs(nid: str) -> bool:
        color[nid] = 1
        for e in m.out_edges(nid):
            c = color.get(e.dst, 0)
            if c == 1:
                return True
            if c == 0 and e.dst in m.nodes and dfs(e.dst):
                return True
        color[nid] = 2
        return False

    return any(color.get(nid, 0) == 0 and dfs(nid) for nid in m.nodes)


def _goal_reachable(m: TaskMap) -> bool:
    """目标节点是否可达(从任一起点沿边方向)"""
    if "n1" not in m.nodes:
        return True
    seen: set[str] = set()
    stack = [nid for nid, n in m.nodes.items() if n.kind == NodeKind.START] or ["n0"]
    while stack:
        cur = stack.pop()
        if cur == "n1":
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(e.dst for e in m.out_edges(cur) if e.dst not in seen)
    return False


def apply_delta(
    m: TaskMap,
    delta: dict[str, Any],
    *,
    worker: str = "",
    dispatch_id: str = "",
) -> tuple[bool, str, list[str]]:
    """校验并应用一条 delta。

    原子性: propose 走 `graph.apply_ops`(整批原子); 节点状态/证据在 propose 通过后才落。
    幂等: 若给出 dispatch_id, 必须等于节点当前 dispatch_id(否则视为陈旧/非本 attempt)。

    Returns:
        (ok, message, warnings)。ok=False 时图不变。
    """
    warnings: list[str] = []
    if not isinstance(delta, dict):
        return False, "delta 不是对象", warnings

    nid = str(delta.get("node", "")).strip()
    node = m.nodes.get(nid)
    if node is None:
        return False, f"节点不存在: {nid!r}", warnings

    # 校验 1: 归属 / 幂等
    if dispatch_id and node.dispatch_id != dispatch_id:
        return False, f"陈旧或非本 attempt(节点 {nid} 当前 dispatch_id={node.dispatch_id!r})", warnings
    if worker and node.owner and node.owner != worker:
        return False, f"非本 worker 持有(节点 {nid} owner={node.owner!r})", warnings

    status = str(delta.get("status", "")).strip()
    if status and status not in _VALID_STATUS:
        return False, f"非法 status: {status!r}(可选 {sorted(_VALID_STATUS)})", warnings

    # ── propose → ops ──
    propose = delta.get("propose") or {}
    if not isinstance(propose, dict):
        return False, "propose 必须是对象", warnings
    pnodes = propose.get("nodes") or []
    pedges = propose.get("edges") or []
    if not isinstance(pnodes, list) or not isinstance(pedges, list):
        return False, "propose.nodes/edges 必须是数组", warnings

    existing = set(m.nodes)
    allowed = {nid}  # 校验 6: 只能碰自己节点 + 本次新建
    lid_map: dict[str, str] = {}
    base = m.next_nid
    ops: list[dict[str, Any]] = []

    for i, pn in enumerate(pnodes):
        if not isinstance(pn, dict):
            return False, f"propose.nodes[{i}] 不是对象", warnings
        lid = str(pn.get("lid", f"x{i}")).strip() or f"x{i}"
        desc = str(pn.get("desc", "")).strip()
        if not desc:
            return False, f"propose.nodes[{i}] 缺 desc", warnings
        kind = str(pn.get("kind", NodeKind.STEP.value))
        if kind not in {k.value for k in NodeKind}:
            return False, f"propose.nodes[{i}] 非法 kind: {kind!r}", warnings
        # 校验 5: 身份去重(desc 完全一致 → 复用已有节点)
        dup = next((x for x in m.nodes.values() if x.desc == desc), None)
        if dup is not None:
            lid_map[lid] = dup.id
            allowed.add(dup.id)
            continue
        gid = f"n{base + len([o for o in ops if o['op'] == 'add_node'])}"
        lid_map[lid] = gid
        allowed.add(gid)
        ops.append({"op": "add_node", "desc": desc, "kind": kind})

    for i, pe in enumerate(pedges):
        if not isinstance(pe, dict):
            return False, f"propose.edges[{i}] 不是对象", warnings
        src = _resolve(str(pe.get("from", "")).strip(), lid_map, existing)
        dst = _resolve(str(pe.get("to", "")).strip(), lid_map, existing)
        if src is None or dst is None:
            return False, f"propose.edges[{i}] 端点无法解析: {pe.get('from')!r}->{pe.get('to')!r}", warnings
        # 校验 6: 作用域——端点必须在自己节点或本次新建内
        if src not in allowed or dst not in allowed:
            return False, f"propose.edges[{i}] 越界(只能连自己节点或本次新建节点): {src}->{dst}", warnings
        sem = str(pe.get("semantic", "")).strip()
        if not sem:
            return False, f"propose.edges[{i}] 缺 semantic", warnings
        ops.append({"op": "add_edge", "from": src, "to": dst, "semantic": sem, "label": str(pe.get("label", ""))})

    # 校验 2: outcome 必须是已有出边语义或本次新建
    outcome = str(delta.get("outcome", "")).strip()
    if status == "done" and not outcome:
        return False, "status=done 时 outcome 必填", warnings
    if outcome:
        have = {e.semantic for e in m.out_edges(nid)}
        new_sem = {str(o["semantic"]) for o in ops if o.get("op") == "add_edge"}
        if outcome not in have and outcome not in new_sem:
            return False, f"outcome {outcome!r} 既非已有出边语义, 也未在 propose 中新建", warnings

    # ── 应用 propose(全有或全无) ──
    if ops:
        try:
            apply_ops(m, ops, by=worker or "worker")
        except GraphOpError as e:
            return False, f"propose 校验失败: {e}", warnings

    # ── 应用节点结果(outcome 一律落) ──
    node = m.nodes[nid]  # apply_ops 替换了 nodes, 需重取
    by = worker or "worker"
    if status:
        node.status = _STATUS_MAP[status]
        node.log(f"status={status}", by=by)
    if outcome:
        node.log(f"outcome={outcome}", by=by)
    for ev in delta.get("evidence") or []:
        node.evidence.append(str(ev))
    if delta.get("evidence"):
        node.log("evidence+", by=by)
    for q in delta.get("questions") or []:
        m.questions.append({"q": str(q), "resolved": False, "ts": time.time(), "by": by})

    # 释放锁
    node.owner = None
    node.dispatch_id = None
    node.lease_until = None
    m.touch()

    # 校验 7/8: 告警
    if _has_cycle(m):
        warnings.append("图存在环(若为修复支线可接受)")
    if not _goal_reachable(m):
        warnings.append("⚠ 目标节点不可达(可能是跑偏信号)")

    sid = delta.get("suggest_next")
    tail = f"; 建议下一步 {sid}" if sid else ""
    return True, f"已应用节点 {nid} 的回报(新增 {len(ops)} 项){tail}", warnings
