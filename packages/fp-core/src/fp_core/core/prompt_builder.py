"""
PromptBuilder — 系统提示词构建器

职责：
- 组装系统提示词：基础提示 → 长期记忆索引
- 技能已迁移到 memory 系统，通过 memory_read 按需检索
- 不直接操作 Agent 或 ConversationState
"""

from typing import cast


class PromptBuilder:
    """系统提示词构建器"""

    def __init__(self):
        pass

    def build_system_prompt(self) -> str:
        """构建完整的系统提示词"""
        parts: list[str] = []

        # 基础 Agent 提示词
        from fp_core.prompts.agent import load_agent_prompt

        agent_prompt = load_agent_prompt()
        if agent_prompt:
            parts.append(agent_prompt)

        # 长期记忆索引（name + type 摘要，不含正文）
        memory_index = self._build_memory_index()
        if memory_index:
            parts.append(memory_index)

        return "\n\n".join(parts)

    @staticmethod
    def _parse_frontmatter(content: str) -> dict[str, str]:
        """解析 YAML frontmatter（第一对 --- 之间）。

        优先用 yaml.safe_load（正确解析多行/引号/特殊字符），
        失败时降级为行扫描（兼容含有二进制字符的旧文件）。
        """
        import yaml

        lines = content.split("\n")
        if not lines or lines[0].strip() != "---":
            return {}
        end = 1
        while end < len(lines) and lines[end].strip() != "---":
            end += 1
        if end >= len(lines):
            return {}
        fm_text = "\n".join(lines[1:end])
        try:
            fm = yaml.safe_load(fm_text)
            if isinstance(fm, dict):
                return cast(dict[str, str], fm)
        except Exception:
            pass
        # 降级：行扫描
        result: dict[str, str] = {}
        for line in fm_text.split("\n"):
            if line.startswith("name:"):
                result["name"] = line.split(":", 1)[1].strip()
            elif line.startswith("type:"):
                result["type"] = line.split(":", 1)[1].strip()
            elif line.startswith("description:"):
                result["description"] = line.split(":", 1)[1].strip()
        return result

    def _build_memory_index(self) -> str:
        """level-0 根索引注入（设计：/tmp/fp_memory_tree_design_20261009.md）

        - 会话构建时确保派生缓存新鲜（生成/刷新 _index.md）并读盘——用户手改的 _index.md 优先
        - 注入预算：类别完整 + 热键，O(1) 不随记忆数增长（vs 旧方案全量叶子名 O(n)）
        - 会话内冻结：本方法只在 build_system_prompt / rebuild_system_prompt 调用，
          会话中途的分裂/合并/热键变化下个会话才生效（保 prefix cache）
        """
        from fp_core import memory_tree

        parts = [p for p in (memory_tree.read_index("~"), memory_tree.read_index(".")) if p]
        return "\n".join(parts)
