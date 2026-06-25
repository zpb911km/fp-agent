"""memory 命令 — 查看持久化记忆"""

import os

from fp_core import config

name = "memory"
aliases = []
description = "查看持久化记忆"


def execute(agent, arg: str) -> tuple[bool, str]:
    lines = ["## 📋 记忆列表", ""]
    memory_dir = config.MEMORY_DIR
    if os.path.isdir(memory_dir):
        for f in sorted(os.listdir(memory_dir)):
            if f.endswith(".md"):
                name = f[:-3]
                lines.append(f"- {name}")
    if len(lines) == 2:
        lines.append("（暂无记忆）")
    output = "\n".join(lines)
    hint = "💡 使用 `memory_read` / `memory_save` 工具管理记忆"

    return (True, output + "\n" + hint)
