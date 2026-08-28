"""exit 命令 — 退出程序"""

from fp_core.core.state import State

name = "exit"
aliases = ["quit"]
description = "退出程序"


def execute(state: State, arg: str) -> tuple[bool, str]:
    raise SystemExit()
