"""fp_multiagent — 多智能体编排的**库**(非插件)

**内聚于 agent_orchestrator 插件包内**: ext 的分发粒度是插件目录, 库放包内
才能随插件一起被分发, 保证插件**自包含**。
库模块 ≠ 插件: 不注册钩子/工具/命令, 无生命周期语义。

依赖方向: 插件 → 库; 跨插件 = 0 条边。
任务图模型不在本子包内, 而在核心库 `fp_core.taskmap`(两个插件共享 → 必须下沉为库)。

模块:
- role    AgentRole 协议(dataclass, duck-typed, 可 JSON 序列化)
- worker  子进程 worker 的 spawn(复用 subagent 机制 + role 传递)
- scheduler  TaskGraph 状态机 + DAG 调度循环
- runs    产物存储(worker 完整输出归档, 比会话长)
"""

from .role import AgentRole
from .runs import (
    RUNS_ROOT,
    delete_run,
    list_runs,
    load_run,
    run_path,
    save_run,
    upsert_entry,
)
from .scheduler import RunPolicy, Task, TaskGraph, TaskState, run_graph
from .worker import spawn_worker

__all__ = [
    "RUNS_ROOT",
    "AgentRole",
    "RunPolicy",
    "Task",
    "TaskGraph",
    "TaskState",
    "delete_run",
    "list_runs",
    "load_run",
    "run_graph",
    "run_path",
    "save_run",
    "spawn_worker",
    "upsert_entry",
]
