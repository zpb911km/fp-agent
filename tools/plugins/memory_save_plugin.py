"""
Memory Save 插件 - 保存长期记忆

用于跨会话持久化用户偏好、项目信息、重要决策等。
"""

from datetime import datetime
import os
from pathlib import Path
from typing import Dict, Any

from ._plugin_config import get_memory_dir


# ── 插件定义（OpenAI function calling schema） ──────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "memory_save",
        "description": "保存一条长期记忆（跨会话持久化）。适合记住用户偏好、项目关键信息、重要决策等。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "记忆名称（英文短词，如 user_role, project_goal）"},
                "type": {
                    "type": "string",
                    "enum": ["user", "project", "feedback", "reference"],
                    "description": "记忆类型：user=用户信息，project=项目状态，feedback=反馈偏好，reference=参考资料",
                },
                "description": {"type": "string", "description": "一句话描述，用于检索"},
                "content": {"type": "string", "description": "记忆正文内容"},
            },
            "required": ["name", "type", "description", "content"],
        },
    },
}




def execute(params: dict) -> str:
    """
    保存记忆
    
    Args:
        params: 包含 name, type, description, content 的字典
        
    Returns:
        保存结果
    """
    name = params.get("name", "")
    mem_type = params.get("type")
    description = params.get("description")
    content = params.get("content", "")
    
    # 验证参数
    required = ["name", "type", "description", "content"]
    for field in required:
        if not params.get(field):
            raise ValueError(f"memory_save 需要以下参数：{', '.join(required)}")
    
    valid_types = ["user", "project", "feedback", "reference"]
    if mem_type not in valid_types:
        raise ValueError(f"type 必须是 {valid_types} 之一")
    
    memory_dir = get_memory_dir()
    os.makedirs(memory_dir, exist_ok=True)
    
    # 格式化名称
    safe_name = name.replace(" ", "_").replace("/", "_")
    
    # 创建文件路径
    path = Path(memory_dir) / f"{safe_name}.md"
    date = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # 写入 YAML frontmatter + 正文
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f"---\n")
        f.write(f"name: {safe_name}\n")
        f.write(f"description: {description}\n")
        f.write(f"type: {mem_type}\n")
        f.write(f"created: {date}\n")
        f.write(f"---\n\n")
        f.write(content + "\n")
    
    return f"✅ 记忆已保存：{safe_name} ({mem_type})"
