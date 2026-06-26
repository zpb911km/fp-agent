"""model 命令 — 显示当前模型配置"""

from fp_core import config

name = "model"
aliases = []
description = "显示当前模型配置"


def execute(agent, arg: str) -> tuple[bool, str]:
    lines = [
        "## ⚙️ 模型配置",
        "",
        f"- **模型**: {agent.model}",
        f"- **温度**: {config.LLM_TEMPERATURE}",
        f"- **最大 Token**: {config.LLM_MAX_TOKENS}",
        f"- **会话目录**: `{config.SESSIONS_DIR}`",
    ]
    output = "\n".join(lines)
    return (True, output)
