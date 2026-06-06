"""skills 命令 — 列出所有技能"""

import display


name = "skills"
aliases = []
description = "列出所有技能"


def execute(agent, arg: str) -> bool:
    display.info(agent.list_skills())
    return True
