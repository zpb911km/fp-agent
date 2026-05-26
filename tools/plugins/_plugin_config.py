"""
插件公共配置 - 从顶层 config 模块获取路径等配置

插件统一通过此模块获取配置，而不是直接 import config 或硬编码路径。
"""

import os
import config


def get_tasks_file() -> str:
    """获取任务文件路径"""
    return config.TASKS_FILE


def get_memory_dir() -> str:
    """获取记忆存储目录"""
    return config.MEMORY_DIR
