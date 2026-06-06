"""
Agent v2 入口模块 — Five Pebbles Agent

核心导出：
  Agent / Message / Response          — 主干类
  LifecycleManager / Hook / Context   — 生命周期系统
  Plugin / PluginRegistry             — 插件基类
"""

__version__ = "2.0.0"
__license__ = "MIT"
__author__ = "zpb"
__description__ = "基于生命周期钩子的插件化 Agent 框架"

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