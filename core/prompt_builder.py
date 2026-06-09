"""
PromptBuilder — 系统提示词构建器

职责：
- 组装系统提示词：基础提示 → 技能提示 → 运行时状态信息
- 持有 SkillLoader 引用
- 不直接操作 Agent 或 ConversationState
"""

import os
from datetime import datetime
from typing import Optional, Any


class PromptBuilder:
    """系统提示词构建器"""

    def __init__(self, skill_loader: Optional[Any] = None):
        """
        Args:
            skill_loader: SkillLoader 实例。None 时使用 skills.loader.skill_loader（全局单例）
        """
        if skill_loader is not None:
            self._skill_loader = skill_loader
        else:
            from skills.loader import skill_loader as default_loader
            self._skill_loader = default_loader

    @property
    def skill_loader(self):
        return self._skill_loader

    def build_system_prompt(self) -> str:
        """构建完整的系统提示词"""
        parts = []

        # 基础 Agent 提示词
        from prompts.agent import load_agent_prompt
        agent_prompt = load_agent_prompt()
        if agent_prompt:
            parts.append(agent_prompt)

        # 技能提示词
        skill_text = self._skill_loader.get_all_prompt_text()
        if skill_text:
            parts.append(skill_text)

        # 运行时状态信息
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current_path = os.getcwd()
        try:
            current_user = os.getlogin()
        except Exception:
            current_user = "unknown"

        state_info = f"""
## 当前时间,路径等状态信息
当前时间: {current_time}
当前路径: {current_path}
当前用户: {current_user}
"""
        parts.append(state_info)

        return "\n\n".join(parts)

    def reload_skills(self) -> int:
        """热重载技能，返回加载的技能数"""
        self._skill_loader.reload()
        return len(self._skill_loader.skills)

    def get_skills_count(self) -> int:
        return len(self._skill_loader.skills)
