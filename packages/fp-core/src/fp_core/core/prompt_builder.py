"""
PromptBuilder — 系统提示词构建器

职责：
- 组装系统提示词：仅基础 Agent 提示词
- 插件扩展段（任务系统 / 邻居 / 记忆根索引）经 ON_INIT 的
  system_prompt_append 注入，rebuild 时由 State.prompt_appends 重放——
  编排层不感知任何具体插件（记忆逻辑已全部收进 plugins/memory/）
- 不直接操作 Agent 或 ConversationState
"""

from fp_core.prompts.agent import load_agent_prompt


class PromptBuilder:
    """系统提示词构建器"""

    def __init__(self):
        pass

    def build_system_prompt(self) -> str:
        """构建基础系统提示词（插件段由 system_prompt_append 机制追加）"""
        return load_agent_prompt()
