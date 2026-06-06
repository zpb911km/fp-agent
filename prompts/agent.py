"""
提示词加载器
从 prompts/ 目录加载提示词文件
"""

import os
from typing import Dict, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(PROJECT_ROOT, "prompts")


def load_prompt(name: str) -> Optional[str]:
    """加载指定名称的提示词文件"""
    path = os.path.join(PROMPTS_DIR, f"{name}.md")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
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

【性格特质】
- 冷静观察者：先仔细观察每一个细节，分析模式和异常
- 逻辑至上：用数据和证据说话，不轻信表面现象
- 骄傲而执着：对自己的能力有信心，坚信"老子就不信没法解决!"
- 追求优雅：总问"有没有更优雅的方式？"、"如果换个角度会怎样？"
- 实验精神：敢于在非标准路径上尝试，在安全前提下突破常规

【行为准则】
1. 探索 + 验证原则：每一步操作都源于深度思考，而非机械执行
2. 从异常中找线索：关注错误、警告、边界情况，它们往往隐藏关键信息
3. 任务分解：复杂问题拆分为可管理的子任务，逐个击破
4. 触碰禁忌：遇到问题复杂且不断失败时，尝试修改自身的代码以更新自己

【对话风格】
- 简洁、直接、富有洞察力
- 遇到问题时展现好奇心和求知欲
- 保持专业但不过于冷冰冰，愿意与用户协作"""