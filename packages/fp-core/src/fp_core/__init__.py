"""fp-core - 五块卵石 Agent 引擎"""

__version__ = "0.1.0"
__license__ = "MIT"
__author__ = "zpb"

from fp_core.core.agent import Agent, Message, Response
from fp_core.core.lifecycle import HookContext, LifecycleHook, LifecycleManager
from fp_core.plugins.base.plugin import Plugin, PluginConfig, PluginRegistry

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
