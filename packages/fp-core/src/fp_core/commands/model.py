"""model 命令 — 显示当前模型配置

读 state.model_name + config，不再经过 Agent.model 中转。
"""

from fp_core import config

name = "model"
aliases = []
description = "显示当前模型配置"


def execute(state, arg: str) -> tuple[bool, str]:
    lines = [
        "## ⚙️ 模型配置",
        "",
        f"- **模型**: {state.model_name}",
        f"- **温度**: {config.LLM_TEMPERATURE}",
        f"- **最大 Token**: {config.LLM_MAX_TOKENS}",
        f"- **会话目录**: `{config.SESSIONS_DIR}`",
    ]
    return (True, "\n".join(lines))
