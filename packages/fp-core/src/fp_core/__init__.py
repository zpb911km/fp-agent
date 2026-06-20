"""fp-core - 五块卵石 Agent 引擎"""

__license__ = "MIT"
__author__ = "zpb"

# 版本号由 setuptools-scm 从 git tag 自动派生
# 优先级: importlib.metadata (已安装时) > _version.py (未安装/构建兜底) > 0.0.0.dev0
try:
    from importlib.metadata import version as _metadata_version

    __version__ = _metadata_version("fp-core")
except Exception:
    try:
        from ._version import __version__  # type: ignore[import-untyped]
    except Exception:
        __version__ = "0.0.0.dev0"

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
