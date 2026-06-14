"""model 命令 — 显示当前模型配置"""

from fp_core import config, display

name = "model"
aliases = []
description = "显示当前模型配置"


def execute(agent, arg: str) -> tuple[bool, str]:
    lines = [
        f"模型:   {agent.model}",
        f"温度:   {config.LLM_TEMPERATURE}",
        f"最大 Token: {config.LLM_MAX_TOKENS}",
        f"会话目录: {config.SESSIONS_DIR}",
    ]
    output = "\n".join(lines)

    # CLI 模式
    for line in lines:
        display.info(line)

    # WebUI 模式
    return (True, output)
