"""remove_skill 命令 — 删除指定技能"""

import display


name = "remove_skill"
aliases = []
description = "删除指定技能"


def execute(agent, arg: str) -> bool:
    agent._cmd_remove_skill(arg)
    return True
