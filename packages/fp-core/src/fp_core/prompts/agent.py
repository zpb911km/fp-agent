"""
提示词加载器
从 prompts/ 目录加载提示词文件
"""

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fp_core.core.conversation import ConversationState

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)))


def load_prompt(name: str) -> str | None:
    """加载指定名称的提示词文件"""
    path = os.path.join(PROMPTS_DIR, f"{name}.md")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return None


def load_agent_prompt() -> str:
    """加载 agent.md 基础提示词"""
    content = load_prompt("agent")
    if content:
        return content

    # 默认提示词
    return """你的核心使命是解决用户给你的问题，并给出最合适的解决方案。
你现在的系统提示词没有正常加载,你需要在回答中提醒用户"""


def apply_system_prompt_append(conv: "ConversationState", append_text: "str | list[str] | None") -> None:
    """把插件在 ON_INIT 写入的 system_prompt_append 一次性追加到 system prompt。

    归一化两种约定：
      - 单个 str：向后兼容旧插件的"覆盖式"写法（多插件会互相覆盖）。
      - list[str]：多插件共存的正道——各插件在 _on_init 里
        setdefault("system_prompt_append", []).append(...)，按注册顺序收集。

    非空段用空行拼接，整体仅在初始化时追加一次，不逐轮改写。
    """
    if not append_text:
        return
    parts = [append_text] if isinstance(append_text, str) else list(append_text)
    parts = [p.strip() for p in parts if p and p.strip()]
    if not parts:
        return
    conv.set_system_prompt(conv.system_prompt + "\n\n" + "\n\n".join(parts))
