"""agent_orchestrator — 多智能体编排插件(核心)

注册 `agent_dispatch` 工具: 调度 worker 子进程跑任务 DAG; 可选把 worker 回报
落到底层任务图(核心库 `fp_core.taskmap`)。

依赖: 核心库 `fp_core.taskmap` + 内聚库 `.fp_multiagent`。
插件 → 库 = 允许; 插件 → 插件 = 0 边。

子 agent 环境(FP_IS_SUBAGENT=1)自禁: worker 不重复注册 agent_dispatch。
"""

from .plugin import OrchestratorPlugin

__all__ = ["OrchestratorPlugin"]
