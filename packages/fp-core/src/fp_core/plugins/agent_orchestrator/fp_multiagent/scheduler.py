"""TaskGraph 调度 — DAG 状态机 + 并发控制循环（库）

核心价值：**DAG 拓扑推进**（asyncio.gather 做不到——那是全并发等齐），
用 asyncio.wait(FIRST_COMPLETED) 支持「阶梯式」并发：某任务一完成，
其下游立即就绪并抢占并发槽，无需等其他无关分支。

状态机：
    PENDING ─deps 全 DONE→ READY ─获槽→ RUNNING ─┬→ DONE
                                                 ├→ FAILED ─retry 未尽→ RUNNING
                                                 └→（上游 FAILED/SKIPPED 且策略=skip）→ SKIPPED

三种模式（mode）：
- supervisor：各任务独立，互不注入上下文（我→N worker→我）
- pipeline  ：下游自动注入上游 artifact 作为背景（A→B→C）
- debate    ：MVP 阶段退化为 supervisor（多轮收敛留待阶段4）
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .role import AgentRole


class TaskState(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass
class Task:
    id: str
    task: str
    role: AgentRole | None = None
    deps: list[str] = field(default_factory=list)
    context: str = ""
    timeout: int = 300
    # 运行时
    state: TaskState = TaskState.PENDING
    effective_context: str = ""
    artifact: str = ""
    error: str = ""
    duration: float = 0.0
    attempts: int = 0
    worker_sid: str = ""


@dataclass
class RunPolicy:
    mode: str = "supervisor"
    max_concurrency: int = 3
    fail_policy: str = "fail_fast"  # fail_fast | skip | retry
    retry: int = 0
    budget: int = 0  # 保留：全局 token 预算（0=不限，MVP 暂只记账不拦截）


@dataclass
class TaskGraph:
    root_id: str
    tasks: list[Task]
    policy: RunPolicy = field(default_factory=RunPolicy)

    def by_id(self) -> dict[str, Task]:
        return {t.id: t for t in self.tasks}


# ── 校验 ────────────────────────────────────────────


def _validate(graph: TaskGraph) -> None:
    ids = {t.id for t in graph.tasks}
    for t in graph.tasks:
        for d in t.deps:
            if d not in ids:
                raise ValueError(f"任务 {t.id} 依赖不存在: {d}")
        if t.id in t.deps:
            raise ValueError(f"任务 {t.id} 自依赖")
    # 环检测（DFS 三色）
    color: dict[str, int] = {}
    by_id = graph.by_id()

    def dfs(nid: str) -> None:
        color[nid] = 1
        for d in by_id[nid].deps:
            c = color.get(d, 0)
            if c == 1:
                raise ValueError(f"依赖存在环: {nid} → {d}")
            if c == 0:
                dfs(d)
        color[nid] = 2

    for t in graph.tasks:
        if color.get(t.id, 0) == 0:
            dfs(t.id)


# ── 调度主循环 ──────────────────────────────────────


async def run_graph(
    graph: TaskGraph,
    spawn: Callable[[Task], Awaitable[dict[str, Any]]],
    *,
    on_update: Callable[[Task], Awaitable[None]] | Callable[[Task], None] | None = None,
) -> TaskGraph:
    """按 DAG 推进执行；spawn(task) 返回 {status,result,error,duration,sid}"""
    _validate(graph)
    by_id = graph.by_id()
    policy = graph.policy
    limit = max(1, policy.max_concurrency)
    sem = asyncio.Semaphore(limit)
    running: dict[str, asyncio.Task[Task]] = {}
    aborted = False

    async def notify(t: Task) -> None:
        if on_update is not None:
            res = on_update(t)
            if inspect.isawaitable(res):
                await res

    async def run_one(t: Task) -> Task:
        async with sem:
            t.state = TaskState.RUNNING
            await notify(t)
            max_attempts = max(1, policy.retry + 1)
            while t.attempts < max_attempts:
                t.attempts += 1
                res = await spawn(t)
                t.artifact = str(res.get("result", "") or "")
                t.duration += float(res.get("duration", 0.0) or 0.0)
                t.worker_sid = str(res.get("sid", "") or "")
                status = res.get("status", "error")
                if status in ("ok", "warning"):
                    t.state = TaskState.DONE
                    t.error = ""
                    break
                t.error = str(res.get("error", "") or "未知错误")
            else:
                t.state = TaskState.FAILED
            await notify(t)
            return t

    while True:
        # 1) 上游失败 → 下游按策略 SKIPPED（仅 skip 策略；fail_fast 直接中止）
        if policy.fail_policy == "skip":
            for t in graph.tasks:
                if t.state == TaskState.PENDING and any(
                    by_id[d].state in (TaskState.FAILED, TaskState.SKIPPED) for d in t.deps
                ):
                    t.state = TaskState.SKIPPED
                    await notify(t)

        # 2) 计算就绪（deps 全 DONE）
        ready = [
            t
            for t in graph.tasks
            if t.state == TaskState.PENDING and all(by_id[d].state == TaskState.DONE for d in t.deps)
        ]

        # 3) 抢占并发槽（阶梯并发：有槽就发，不等同层其他任务）
        while ready and len(running) < limit:
            t = ready.pop(0)
            t.effective_context = _effective_context(t, by_id, policy.mode)
            t.state = TaskState.READY
            running[t.id] = asyncio.create_task(run_one(t))

        # 4) 无可运行任务 → 结束
        if not running:
            break

        # 5) 等最先完成者
        done, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
        for fut in done:
            finished: Task = fut.result()
            running.pop(finished.id, None)
            if finished.state == TaskState.FAILED and policy.fail_policy == "fail_fast":
                aborted = True
                for r in running.values():
                    r.cancel()
                for r in running.values():
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await r
                running.clear()
                break

        if aborted:
            # 未开始的任务标记 SKIPPED
            for t in graph.tasks:
                if t.state == TaskState.PENDING:
                    t.state = TaskState.SKIPPED
                    await notify(t)
            break

    return graph


def _effective_context(t: Task, by_id: dict[str, Task], mode: str) -> str:
    """pipeline 模式下把上游 artifact 注入下游背景"""
    if mode != "pipeline":
        return t.context
    parts: list[str] = []
    if t.context:
        parts.append(t.context)
    for d in t.deps:
        up = by_id[d]
        if up.artifact:
            parts.append(f"[上游 {d} 产出]\n{up.artifact}")
    return "\n\n".join(parts)
