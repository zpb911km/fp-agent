"""compact 命令 — 压缩对话历史"""

import display


name = "compact"
aliases = []
description = "压缩对话历史"


def execute(agent, arg: str) -> bool:
    agent._compact_context()
    return True
