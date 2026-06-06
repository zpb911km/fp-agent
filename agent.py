"""
Agent v2 入口模块

核心导出：
  Agent / Message / Response          — 主干类
  LifecycleManager / Hook / Context   — 生命周期系统
  Plugin / PluginRegistry             — 插件基类
"""

from core.agent import Agent, Message, Response
from core.lifecycle import LifecycleManager, LifecycleHook, HookContext
from plugins.base.plugin import Plugin, PluginConfig, PluginRegistry

__all__ = [
    "Agent",
    "Message",
    "Response",
    "LifecycleManager",
    "LifecycleHook",
    "HookContext",
    "Plugin",
    "PluginConfig",
    "PluginRegistry",
]