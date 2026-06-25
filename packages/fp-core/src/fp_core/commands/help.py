"""help 命令 — 显示帮助信息"""

name = "help"
aliases = ["?"]
description = "显示此帮助"


def execute(agent, arg: str) -> tuple[bool, str]:
    from fp_core.commands import get_all_commands

    cmds = get_all_commands()
    lines = ["## 可用命令"]
    for c, d in sorted(cmds.items()):
        lines.append(f"- `/{c}` — {d}")
    output = "\n".join(lines)

    return (True, output)
