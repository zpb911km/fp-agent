"""reload_skills 命令 — 热重载技能"""

import display


name = "reload_skills"
aliases = []
description = "热重载技能"


def execute(agent, arg: str) -> bool:
    if agent.reload_skills():
        agent._context[0]["content"] = agent._system_prompt
    return True
