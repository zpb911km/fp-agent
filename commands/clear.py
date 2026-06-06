"""clear 命令 — 清空当前会话"""

import display


name = "clear"
aliases = []
description = "清空当前会话"


def execute(agent, arg: str) -> bool:
    agent.clear_session()
    display.info("🧹 当前会话已清空")
    return True
