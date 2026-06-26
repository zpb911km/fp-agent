"""exit 命令 — 退出程序"""

name = "exit"
aliases = ["quit"]
description = "退出程序"


def execute(agent, arg: str) -> tuple[bool, str]:
    raise SystemExit()
