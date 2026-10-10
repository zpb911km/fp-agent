"""memory — 记忆系统插件（树布局 / FTS5 检索 / 读写工具 / 巩固机检）

系统整体收拢于此包，不再散落 fp_core 根、tools/extensions 与独立插件目录。
入口：MemoryPlugin（PluginRegistry 目录扫描经本 __init__ 发现）。
"""

from .plugin import MemoryPlugin

__all__ = ["MemoryPlugin"]
