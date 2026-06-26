"""
PromptBuilder — 系统提示词构建器

职责：
- 组装系统提示词：基础提示 → 运行时状态信息 → 长期记忆索引
- 技能已迁移到 memory 系统，通过 memory_read 按需检索
- 不直接操作 Agent 或 ConversationState
"""

import os
from datetime import datetime

from fp_core.platform_utils import check_git_bash, is_windows, platform


class PromptBuilder:
    """系统提示词构建器"""

    def __init__(self):
        pass

    def build_system_prompt(self) -> str:
        """构建完整的系统提示词"""
        parts = []

        # 基础 Agent 提示词
        from fp_core.prompts.agent import load_agent_prompt

        agent_prompt = load_agent_prompt()
        if agent_prompt:
            parts.append(agent_prompt)

        # 运行时状态信息
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current_path = os.getcwd()
        try:
            current_user = os.getlogin()
        except Exception:
            current_user = "unknown"

        # ── 运行时环境信息 ──
        runtime_parts = [
            f"当前时间: {current_time}",
            f"当前路径: {current_path}",
            f"当前用户: {current_user}",
            f"当前平台: {platform()}",
        ]
        if is_windows():
            git_ok, git_msg = check_git_bash()
            if git_ok:
                runtime_parts.append("Git Bash: 可用 ✓（写 Unix 命令：ls/grep/awk）")
            else:
                runtime_parts.append("Git Bash: 不可用 ✗（使用 cmd.exe 回退，写 Windows 命令：dir/type/findstr）")
        runtime_info = "\n".join(runtime_parts)
        state_info = f"\n## 当前时间,路径等状态信息\n{runtime_info}\n"
        parts.append(state_info)

        # 长期记忆索引（name + type 摘要，不含正文）
        memory_index = self._build_memory_index()
        if memory_index:
            parts.append(memory_index)

        return "\n\n".join(parts)

    @staticmethod
    def _parse_frontmatter(content: str) -> dict:
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
                return fm
        except Exception:
            pass
        # 降级：行扫描
        result = {}
        for line in fm_text.split("\n"):
            if line.startswith("name:"):
                result["name"] = line.split(":", 1)[1].strip()
            elif line.startswith("type:"):
                result["type"] = line.split(":", 1)[1].strip()
            elif line.startswith("description:"):
                result["description"] = line.split(":", 1)[1].strip()
        return result

    def _build_memory_index(self) -> str:
        """扫描两棵记忆目录树，返回分类分组的索引（用于 system prompt）"""
        from fp_core import config

        global_dir = config.MEMORY_DIR
        local_dir = os.path.join(os.getcwd(), config.MEMORY_DIR_LOCAL)

        def _scan_dir(memory_dir: str) -> list[dict]:
            if not os.path.isdir(memory_dir):
                return []
            results = []
            for fname in sorted(os.listdir(memory_dir)):
                if not fname.endswith(".md"):
                    continue
                fpath = os.path.join(memory_dir, fname)
                try:
                    with open(fpath, encoding="utf-8") as f:
                        content = f.read()
                except Exception:
                    continue
                fm = PromptBuilder._parse_frontmatter(content)
                mem_name = fm.get("name", "")
                mem_type = fm.get("type", "")
                if mem_name:
                    results.append({"name": mem_name, "type": mem_type or "uncategorized"})
            return results

        global_memories = _scan_dir(global_dir)
        local_memories = _scan_dir(local_dir)

        total = len(global_memories) + len(local_memories)
        if total == 0:
            return ""

        def _group(memories: list[dict]) -> dict[str, list[str]]:
            groups: dict[str, list[str]] = {}
            for m in memories:
                groups.setdefault(m["type"], []).append(m["name"])
            return groups

        lines = ["## 我的长期记忆索引"]
        lines.append(f"共 {total} 条（进入新会话后自动加载，需要详情时使用 memory_read 查询）")
        lines.append("")

        # 全局
        global_groups = _group(global_memories)
        lines.append(f"~/（全局，{len(global_memories)}条）")
        for cat in sorted(global_groups):
            names = ", ".join(global_groups[cat])
            lines.append(f"  {cat}:    {names}")
        lines.append("")

        # 本地
        local_groups = _group(local_memories)
        if local_memories:
            lines.append(f"./（本地 📍 .fp/memory，{len(local_memories)}条）")
            for cat in sorted(local_groups):
                names = ", ".join(local_groups[cat])
                lines.append(f"  {cat}:    {names}")
            lines.append("")

        # 使用引导
        lines.append("━ 用法 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append('精确读取:   memory_read(name="<记忆名>")       ← 从上方列表中直接使用')
        lines.append('搜索:       memory_read(query="<关键词>")')
        lines.append('浏览分类:   memory_read(path="~/<分类>")       ← 或 memory_read(path="./<分类>")')
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        return "\n".join(lines)
