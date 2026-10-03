"""taskmap — 任务图核心库(纯逻辑, 无插件语义)

任务 = 一张有向图(节点=状态, 边=结果语义), 是长任务的脊柱。

被插件共享(遵守「插件 → 库, 而非插件 → 插件」铁律):
- `plugins/task_system`  任务图的工具与生命周期注入

模块:
- models  Node/Edge/TaskMap + 枚举
- store   TaskMapStore(单文件 JSON + 原子写 + 旧格式迁移)
- graph   图变更 op(批量原子)
- render  文本渲染 + 紧凑提醒
- delta   worker delta 协议(解析 / 校验 / 应用)
"""

from .delta import apply_delta, delta_instruction, extract_delta
from .graph import GraphOpError, apply_ops
from .models import (
    GOAL_NODE_ID,
    START_NODE_ID,
    Edge,
    EdgeSemantic,
    MapStatus,
    Node,
    NodeKind,
    NodeStatus,
    TaskMap,
)
from .render import render_full, render_subgraph, summary
from .store import TaskMapStore

__all__ = [
    "GOAL_NODE_ID",
    "START_NODE_ID",
    "Edge",
    "EdgeSemantic",
    "GraphOpError",
    "MapStatus",
    "Node",
    "NodeKind",
    "NodeStatus",
    "TaskMap",
    "TaskMapStore",
    "apply_delta",
    "apply_ops",
    "delta_instruction",
    "extract_delta",
    "render_full",
    "render_subgraph",
    "summary",
]
