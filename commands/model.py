"""model 命令 — 显示当前模型配置"""

import display
import config


name = "model"
aliases = []
description = "显示当前模型配置"


def execute(agent, arg: str) -> bool:
    display.info(f"模型:   {agent.model}")
    display.info(f"温度:   {config.LLM_TEMPERATURE}")
    display.info(f"最大 Token: {config.LLM_MAX_TOKENS}")
    display.info(f"会话目录: {config.SESSIONS_DIR}")
    return True
