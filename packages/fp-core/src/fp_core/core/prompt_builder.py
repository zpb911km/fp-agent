"""
PromptBuilder — 系统提示词构建器

职责：
- 组装系统提示词：基础提示 → 技能提示 → 运行时状态信息
- 持有 SkillLoader 引用
- 不直接操作 Agent 或 ConversationState
"""

import os
from datetime import datetime
from typing import Any


class PromptBuilder:
    """系统提示词构建器"""

    def __init__(self, skill_loader: Any | None = None):
        """
        Args:
            skill_loader: SkillLoader 实例。None 时创建独立实例（不再共享全局单例）
        """
        if skill_loader is not None:
            self._skill_loader = skill_loader
        else:
            from fp_core.skills.loader import create_skill_loader

            self._skill_loader = create_skill_loader()

    @property
    def skill_loader(self):
        return self._skill_loader

    def build_system_prompt(self) -> str:
        """构建完整的系统提示词"""
        parts = []

        # 基础 Agent 提示词
        from fp_core.prompts.agent import load_agent_prompt

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

        # 长期记忆索引（name + type 摘要，不含正文）
        memory_index = self._build_memory_index()
        if memory_index:
            parts.append(memory_index)

        return "\n\n".join(parts)

    def _build_memory_index(self) -> str:
        """扫描记忆目录，返回格式化的记忆索引块（仅 name + type）"""
        from fp_core import config

        memory_dir = config.MEMORY_DIR
        if not os.path.isdir(memory_dir):
            return ""

        lines = ["## 我的长期记忆索引", ""]
        count = 0

        for fname in sorted(os.listdir(memory_dir)):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(memory_dir, fname)
            try:
                with open(fpath, encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue

            mem_name = ""
            mem_type = ""
            in_frontmatter = False
            for line in content.split("\n"):
                if line.strip() == "---":
                    in_frontmatter = not in_frontmatter
                    continue
                if in_frontmatter:
                    if line.startswith("name:"):
                        mem_name = line.split(":", 1)[1].strip()
                    elif line.startswith("type:"):
                        mem_type = line.split(":", 1)[1].strip()

            if mem_name:
                lines.append(f"[{mem_type}] {mem_name}")
                count += 1

        if count == 0:
            return ""

        lines.insert(1, f"共 {count} 条（进入新会话后自动加载，需要详情时使用 memory_read 查询）")
        return "\n".join(lines)

    def reload_skills(self) -> int:
        """热重载技能，返回加载的技能数"""
        self._skill_loader.reload()
        return len(self._skill_loader.skills)

    def get_skills_count(self) -> int:
        return len(self._skill_loader.skills)
