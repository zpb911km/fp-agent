"""OrchestratorPlugin — 多智能体编排器(核心)

通过 ON_INIT 注册 `agent_dispatch` 工具: 给定一个任务 DAG, 并发/按依赖调度多个
worker 子进程(复用 subagent 机制), 返回**结构化 digest**——语义合成留给主 agent。

**与任务图(taskmap)的集成**(单写者落图):
- 可传 `task_id` 把本次派发绑定到某张任务图; 每个带 `node_id` 的 worker 会:
  ① 收到该节点的子图上下文(render_subgraph); ② 完成后其 stdout 的
  `[taskmap-delta]` 块被解析、8 条校验、由**编排器**落图(唯一写者)。
- worker 无图插件、不改图, 只通过 delta **提议**。

依赖: 核心库 `fp_core.taskmap`(任务图模型) + 内聚库 `fp_multiagent`。
插件 → 库 = 允许; 插件 → 插件 = 0 边。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, cast

from fp_core.core.lifecycle import HookContext, LifecycleHook, LifecycleManager
from fp_core.logger import get_logger
from fp_core.plugins.base.plugin import Plugin, PluginConfig
from fp_core.taskmap.delta import apply_delta, delta_instruction, extract_delta
from fp_core.taskmap.models import NodeStatus, TaskMap
from fp_core.taskmap.render import render_subgraph
from fp_core.taskmap.store import TaskMapStore
from fp_core.tools import ToolRegistry
from fp_core.tools.core import OpenAISchema

# ── 内聚库(本插件包内的子包; 随插件目录一起分发) ──
from .fp_multiagent import runs, scheduler
from .fp_multiagent.role import AgentRole
from .fp_multiagent.scheduler import RunPolicy, Task, TaskGraph, TaskState
from .fp_multiagent.worker import spawn_worker

logger = get_logger()

ORCHESTRATOR_DESCRIPTION = """【多智能体编排 agent_dispatch】
派发一个**任务 DAG**, 由编排器并发/按依赖调度多个 worker(各自独立上下文与进程)。

- `tasks`: 数组, 每项 `{id, task, deps?, context?, role?, timeout?, node_id?}`
  - `deps`: 依赖的任务 id(上游 DONE 后才跑); `mode=pipeline` 时下游自动获得上游产出。
  - `node_id`: 该 worker 负责的**任务图节点 id**(配合顶层 `task_id`)。
- `task_id`(可选): 把本次派发绑定到某张任务图。带 `node_id` 的 worker 会收到该节点
  的子图上下文, 其回报由编排器**校验并落图**(单写者)。
- `role`: `{name, system_prompt?, allowed_tools?, llm_model?, temperature?}`。
- `fail_policy`: fail_fast | skip | retry; `max_concurrency` 默认 3。
- 返回**结构化 digest**(每任务 state/artifact/耗时 + 图变更), 不做语义合成。

只给你 digest: 最终结论由你自己聚合。"""

TOOL_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "agent_dispatch",
        "description": (
            "派发一个多智能体任务 DAG: 并发/按依赖调度多个 worker 子进程, 各自独立上下文与进程; "
            "返回结构化 digest。可选 task_id 绑定任务图——带 node_id 的 worker 的回报会被校验并落图。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "description": "必填。worker 的工作目录(bash(pwd) 获取)"},
                "task_id": {
                    "type": "integer",
                    "description": "可选。绑定到某个任务图 ID(见 task_*); 各 task 的 node_id 会在该图上落图。",
                },
                "tasks": {
                    "type": "array",
                    "description": "任务 DAG 节点列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "任务唯一 id"},
                            "task": {"type": "string", "description": "该 worker 要完成的任务, 自包含描述"},
                            "deps": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "依赖的任务 id(全部 DONE 后才执行)",
                            },
                            "context": {"type": "string", "description": "附加背景(可选)"},
                            "node_id": {
                                "type": "string",
                                "description": "该 worker 负责的任务图节点 id(可选, 需配合顶层 task_id)",
                            },
                            "role": {
                                "type": "object",
                                "description": "该 worker 的角色(可选)",
                                "properties": {
                                    "name": {"type": "string"},
                                    "system_prompt": {"type": "string"},
                                    "allowed_tools": {"type": "array", "items": {"type": "string"}},
                                    "llm_model": {"type": "string"},
                                    "temperature": {"type": "number"},
                                    "max_turns": {"type": "integer"},
                                },
                                "required": ["name"],
                            },
                            "timeout": {"type": "integer", "description": "该任务超时秒数(默认 300)"},
                        },
                        "required": ["id", "task"],
                    },
                },
                "mode": {
                    "type": "string",
                    "enum": ["supervisor", "pipeline", "debate"],
                    "description": "supervisor=各任务独立; pipeline=下游注入上游产出; debate=MVP 暂同 supervisor",
                },
                "max_concurrency": {"type": "integer", "description": "并发上限(默认 3)"},
                "fail_policy": {
                    "type": "string",
                    "enum": ["fail_fast", "skip", "retry"],
                    "description": "失败策略(默认 fail_fast)",
                },
                "retry": {"type": "integer", "description": "fail_policy=retry 时的重试次数(默认 0)"},
            },
            "required": ["cwd", "tasks"],
        },
    },
}

_ARTIFACT_PREVIEW = 4000


def _err(msg: str) -> str:
    return json.dumps({"status": "error", "result": msg}, ensure_ascii=False, indent=2)


class OrchestratorPlugin(Plugin):
    """多智能体编排器插件"""

    name = "agent_orchestrator"
    version = "1.0.0"

    def __init__(self, config: PluginConfig | None = None):
        super().__init__(config)
        self._tool_registry: ToolRegistry | None = None
        self._registered_tools: list[str] = []

    # ── 生命周期 ─────────────────────────────────────

    def on_register(self, lifecycle: LifecycleManager):
        # 子 agent 环境自禁: worker 不需要 agent_dispatch(递归也被守卫拒绝)
        if os.environ.get("FP_IS_SUBAGENT") == "1":
            logger.info("[agent_orchestrator] 子 agent 环境, 不加载")
            self.disable()
            return

        lifecycle.register(
            LifecycleHook.ON_INIT,
            self._on_init,
            priority=50,
            name="agent_orchestrator_init",
        )

    def on_unregister(self):
        if self._tool_registry is not None:
            for tool_name in self._registered_tools:
                self._tool_registry.unregister_tool(tool_name)
        self._registered_tools.clear()
        self._tool_registry = None

    # ── 钩子实现 ─────────────────────────────────────

    async def _on_init(self, ctx: HookContext, **kwargs: Any) -> HookContext:
        tool_registry: ToolRegistry | None = kwargs.get("tool_registry")
        if tool_registry is not None:
            self._tool_registry = tool_registry
            tool_registry.register_tool(
                "agent_dispatch",
                cast(OpenAISchema, TOOL_DEFINITION),
                self._execute,
            )
            self._registered_tools.append("agent_dispatch")

        ctx.data.setdefault("system_prompt_append", []).append(ORCHESTRATOR_DESCRIPTION)
        return ctx

    # ── 工具执行 ─────────────────────────────────────

    async def _execute(self, params: dict[str, Any]) -> str:
        cwd = str(params.get("cwd", "") or "").strip()
        raw_tasks: Any = params.get("tasks") or []

        if not cwd or not os.path.isdir(cwd):
            return _err(f"cwd 无效或不存在: {cwd!r}(用 bash(pwd) 获取)")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            return _err("tasks 不能为空")

        # ── 可选: 绑定任务图 ──
        task_id = params.get("task_id")
        tmap: TaskMap | None = None
        store: TaskMapStore | None = None
        if task_id is not None:
            store = TaskMapStore()
            tmap = store.get(task_id)
            if tmap is None:
                return _err(f"未找到任务图 #{task_id}(见 task_list)")

        # ── 构造任务 + node 映射 ──
        tasks: list[Task] = []
        node_of: dict[str, str] = {}
        seen: set[str] = set()
        for item in cast(list[dict[str, Any]], raw_tasks):
            tid = str(item.get("id", "") or "").strip()
            if not tid or tid in seen:
                return _err(f"任务 id 缺失或重复: {tid!r}")
            seen.add(tid)
            role_raw = item.get("role")
            role = AgentRole.from_dict(cast(dict[str, Any], role_raw)) if isinstance(role_raw, dict) else None
            tasks.append(
                Task(
                    id=tid,
                    task=str(item.get("task", "") or ""),
                    role=role,
                    deps=[str(d) for d in (item.get("deps") or [])],
                    context=str(item.get("context", "") or ""),
                    timeout=int(item.get("timeout", 300) or 300),
                )
            )
            nid = str(item.get("node_id", "") or "").strip()
            if nid:
                if tmap is None:
                    return _err(f"任务 {tid} 给了 node_id 但未提供 task_id")
                if nid not in tmap.nodes:
                    return _err(f"任务图 #{task_id} 无节点 {nid}")
                node_of[tid] = nid

        policy = RunPolicy(
            mode=str(params.get("mode", "supervisor") or "supervisor"),
            max_concurrency=int(params.get("max_concurrency", 3) or 3),
            fail_policy=str(params.get("fail_policy", "fail_fast") or "fail_fast"),
            retry=int(params.get("retry", 0) or 0),
        )
        run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        graph = TaskGraph(root_id=run_id, tasks=tasks, policy=policy)
        sink: dict[str, list[str]] = {"applied": [], "warnings": []}

        # ── 产物归档回调 ──
        async def on_update(t: Task) -> None:
            try:
                runs.upsert_entry(
                    run_id,
                    {
                        "task_id": t.id,
                        "agent": "worker",
                        "role": t.role.name if t.role else None,
                        "state": t.state.value,
                        "artifact": t.artifact,
                        "deps": t.deps,
                        "node_id": node_of.get(t.id),
                        "tokens": 0,
                        "duration": round(t.duration, 2),
                        "ts": time.time(),
                        "error": t.error,
                    },
                )
            except Exception as e:  # noqa: BLE001 — 归档失败不应中断编排
                logger.warning(f"[agent_orchestrator] 产物写入失败: {e}")

        # ── worker 生成闭包(注入子图 + 加锁 + 回收 delta) ──
        async def spawn(t: Task) -> dict[str, Any]:
            nid = node_of.get(t.id, "")
            dispatch_id = ""
            ctx_parts: list[str] = []
            if tmap is not None and store is not None and nid:
                dispatch_id = f"{run_id}:{t.id}:{t.attempts}"
                node = tmap.nodes.get(nid)
                if node is not None:
                    node.status = NodeStatus.ACTIVE
                    node.owner = t.id
                    node.dispatch_id = dispatch_id
                    node.lease_until = time.time() + t.timeout
                    node.log(f"dispatch {t.id}", by="orchestrator")
                    store.save_map(tmap)
                ctx_parts.append(render_subgraph(tmap, nid))
                ctx_parts.append(delta_instruction(nid, dispatch_id))
            if t.effective_context:
                ctx_parts.append(t.effective_context)

            res = await spawn_worker(
                task=t.task,
                cwd=cwd,
                context="\n\n".join(ctx_parts),
                role_json=t.role.to_json() if t.role else "",
                timeout=t.timeout,
            )

            if tmap is not None and store is not None and nid:
                self._apply_worker_result(tmap, nid, dispatch_id, res, run_id, t.id, store, sink)
            return res

        try:
            await scheduler.run_graph(graph, spawn, on_update=on_update)
        except ValueError as e:
            return _err(f"任务图非法: {e}")

        return json.dumps(self._digest(graph, run_id, task_id, sink), ensure_ascii=False, indent=2)

    # ── 落图(单写者) ─────────────────────────────────

    @staticmethod
    def _apply_worker_result(
        tmap: TaskMap,
        nid: str,
        dispatch_id: str,
        res: dict[str, Any],
        run_id: str,
        tid: str,
        store: TaskMapStore,
        sink: dict[str, list[str]],
    ) -> None:
        """把 worker 的 delta 校验并落到任务图。

        outcome 一律落; propose 全有或全无。无有效 delta 则按执行结果做**状态兜底**
        (不声称边语义, 由主 agent 后续用 task_edit 补边)。
        """
        text = str(res.get("result", "") or "")
        ok = str(res.get("status", "")) in ("ok", "warning")
        delta, _note = extract_delta(text)
        applied = False

        if delta is not None:
            ok_a, msg, warns = apply_delta(tmap, delta, worker=tid, dispatch_id=dispatch_id)
            sink["warnings"].extend(f"[{tid}] {w}" for w in warns)
            if ok_a:
                applied = True
                sink["applied"].append(f"[{tid}] {msg}")
            else:
                sink["warnings"].append(f"[{tid}] delta 被拒({msg}), 退化为状态兜底")

        if not applied:
            node = tmap.nodes.get(nid)
            if node is not None:
                node.status = NodeStatus.DONE if ok else NodeStatus.FAILED
                node.evidence.append(f"run {run_id} · {res.get('status')}")
                node.owner = None
                node.dispatch_id = None
                node.lease_until = None
                node.log(f"auto-{'done' if ok else 'failed'}(无 delta)", by="orchestrator")
                sink["applied"].append(f"[{tid}] 无有效 delta, 按执行结果置 {node.status.value}")

        store.save_map(tmap)

    # ── digest ───────────────────────────────────────

    @staticmethod
    def _digest(graph: TaskGraph, run_id: str, task_id: Any, sink: dict[str, list[str]]) -> dict[str, Any]:
        states = [t.state.value for t in graph.tasks]
        total_duration = round(sum(t.duration for t in graph.tasks), 2)
        items: list[dict[str, Any]] = []
        for t in graph.tasks:
            artifact = t.artifact
            if len(artifact) > _ARTIFACT_PREVIEW:
                artifact = artifact[:_ARTIFACT_PREVIEW] + f"\n...(已截断, 全文 {len(t.artifact)} 字符, 见 runs)"
            items.append({
                "id": t.id,
                "role": t.role.name if t.role else None,
                "state": t.state.value,
                "duration": round(t.duration, 2),
                "attempts": t.attempts,
                "worker_sid": t.worker_sid,
                "artifact": artifact,
                "error": t.error,
            })
        digest: dict[str, Any] = {
            "status": "ok",
            "run_id": run_id,
            "runs": runs.run_path(run_id),
            "mode": graph.policy.mode,
            "policy": {
                "max_concurrency": graph.policy.max_concurrency,
                "fail_policy": graph.policy.fail_policy,
                "retry": graph.policy.retry,
            },
            "summary": {
                "total": len(graph.tasks),
                "done": states.count(TaskState.DONE.value),
                "failed": states.count(TaskState.FAILED.value),
                "skipped": states.count(TaskState.SKIPPED.value),
                "duration": total_duration,
            },
            "tasks": items,
        }
        if task_id is not None:
            digest["taskmap"] = {
                "task_id": task_id,
                "applied": sink["applied"],
                "warnings": sink["warnings"],
            }
        return digest
