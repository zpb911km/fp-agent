"""memory 命令 — 查看持久化记忆"""

import os

from fp_core import config, display

name = "memory"
aliases = []
description = "查看持久化记忆"


def execute(agent, arg: str) -> tuple[bool, str]:
    lines = ["📋 记忆列表:", ""]
    memory_dir = config.MEMORY_DIR
    if os.path.isdir(memory_dir):
        for f in sorted(os.listdir(memory_dir)):
            if f.endswith(".md"):
                name = f[:-3]
                lines.append(f"  • {name}")
    if len(lines) == 2:
        lines.append("  （暂无记忆）")
    output = "\n".join(lines)
    hint = "💡 使用 memory_read / memory_save 工具管理记忆"

    # CLI 模式：保持着色
    display.info(output)
    display.hint(hint)

    # WebUI 模式：通过返回值传递
    return (True, output + "\n" + hint)
