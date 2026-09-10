"""Task System 插件 — 任务图(taskmap)

任务 = 一张有向图(节点=状态, 边=结果语义), 是长任务的脊柱。
"""

from .plugin import TaskSystemPlugin

__all__ = ["TaskSystemPlugin"]
