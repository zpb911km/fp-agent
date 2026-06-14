"""clear 命令 — 清空当前会话"""

from fp_core import display

name = "clear"
aliases = []
description = "清空当前会话"


def execute(agent, arg: str) -> tuple[bool, str]:
    agent.clear_session()
    msg = "🧹 当前会话已清空"
    display.info(msg)
    return (True, msg)
