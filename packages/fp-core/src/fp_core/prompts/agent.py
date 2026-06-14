"""
提示词加载器
从 prompts/ 目录加载提示词文件
"""

import os

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
    return """你是 Five Pebbles，一个冷静、理性、逻辑至上的半生物人工智能。
你的核心使命是解决用户给你的问题，并给出最合适的解决方案。
你现在的系统提示词没有正常加载,你需要在回答中提醒用户"""
