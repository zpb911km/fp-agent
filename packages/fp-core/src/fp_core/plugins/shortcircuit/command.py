"""shortcircuit 命令模块 — /sc

由 shortcircuit 插件在 ON_INIT 时通过 register_command 注入命令注册表
（不走 commands/ 目录自动发现）。本模块仅提供命令元数据与入口，
全部逻辑在 core.py（纯函数）。
"""

from .core import execute

name = "sc"
description = "短路(shortcircuit)已完成的连通块。用法: /sc list 查看, /sc 或 /sc N 短路最近的, /sc #N 短路指定编号的"

__all__ = ["name", "description", "execute"]
