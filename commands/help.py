"""help 命令 — 显示帮助信息"""

import display

name = "help"
aliases = ["?"]
description = "显示此帮助"


def execute(agent, arg: str) -> tuple[bool, str]:
    from commands import get_all_commands

    cmds = get_all_commands()
    lines = ["可用命令:"]
    for c, d in sorted(cmds.items()):
        lines.append(f"  /{c:11s}  {d}")
    output = "\n".join(lines)

    # CLI 模式：输出到终端（保持着色）
    display.info("可用命令:")
    for c, d in sorted(cmds.items()):
        display.item(f"  /{c:11s}  {d}")

    # WebUI 模式：通过返回值传递输出
    return (True, output)
