"""shortcircuit 命令模块 — /sc

由 shortcircuit 插件在 ON_INIT 时通过 register_command 注入命令注册表
（不走 commands/ 目录自动发现）。本模块仅提供命令元数据与入口，
全部逻辑在 core.py（纯函数）。
"""

from .core import execute

name = "sc"
description = (
    "修剪对话历史连通块。用法: /sc [list] 查看（缺省）; "
    "/sc -d #3-9 退化（合成一对，文字全留）; /sc -c #7 裁剪（只留首问末答）; "
    "/sc -r #1-3 提炼（LLM）; target 支持 #7 / #3-9 / #3,#7-9"
)

__all__ = ["name", "description", "execute"]
