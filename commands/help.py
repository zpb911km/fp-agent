"""help 命令 — 显示帮助信息"""

import display


name = "help"
aliases = ["?"]
description = "显示此帮助"


def execute(agent, arg: str) -> bool:
    from commands import get_all_commands

    cmds = get_all_commands()
    display.info("可用命令:")
    for c, d in sorted(cmds.items()):
        display.item(f"  /{c:11s}  {d}")
    return True
