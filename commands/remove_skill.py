"""remove_skill 命令 — 删除指定技能"""

import display


name = "remove_skill"
aliases = []
description = "删除指定技能"


def execute(agent, arg: str) -> tuple[bool, str]:
    agent._cmd_remove_skill(arg)
    return (True, f"🗑️ 技能 '{arg}' 已删除（如存在）")
