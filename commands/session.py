"""session 命令 — 显示当前会话信息"""

import display

name = "session"
aliases = []
description = "显示当前会话信息"


def execute(agent, arg: str) -> tuple[bool, str]:
    msg = f"📂 当前会话: {agent.session.session_id}"
    display.info(msg)
    return (True, msg)
