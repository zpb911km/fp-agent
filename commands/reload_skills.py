"""reload_skills 命令 — 热重载技能"""


name = "reload_skills"
aliases = []
description = "热重载技能"


def execute(agent, arg: str) -> tuple[bool, str]:
    if agent.reload_skills():
        return (True, "✅ 技能重载完成")
    return (True, "❌ 技能重载失败")
