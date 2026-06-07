"""skills 命令 — 列出所有技能"""

import display


name = "skills"
aliases = []
description = "列出所有技能"


def execute(agent, arg: str) -> tuple[bool, str]:
    output = agent.list_skills()
    display.info(output)
    return (True, output)
