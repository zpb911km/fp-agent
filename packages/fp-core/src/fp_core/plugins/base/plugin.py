"""
插件基类
所有功能插件都继承自此基类
"""

import importlib
import importlib.util
import inspect
import os
import sys
import types
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from fp_core.core.lifecycle import HookContext, LifecycleHook, LifecycleManager
from fp_core.logger import get_logger


@dataclass
class PluginConfig:
    """插件配置基类"""

    enabled: bool = True
    priority: int = 100
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])


class Plugin(ABC):
    """
    插件基类

    所有插件必须:
    1. 继承 Plugin 基类
    2. 实现 on_register() 方法注册自己的钩子
    3. 在适当的地方调用父类的 hook() 方法
    """

    name: str = "base_plugin"
    version: str = "1.0.0"

    def __init__(self, config: PluginConfig | None = None):
        self.config = config or PluginConfig()
        self._lifecycle: LifecycleManager | None = None
        self._enabled = self.config.enabled
        self._state: dict[str, Any] = {}

    @abstractmethod
    def on_register(self, lifecycle: LifecycleManager):
        """
        注册插件钩子
        子类实现此方法来注册自己的生命周期钩子
        """
        pass

    @abstractmethod
    def on_unregister(self):
        """插件卸载时调用"""
        pass

    def enable(self):
        """启用插件"""
        self._enabled = True

    def disable(self):
        """禁用插件"""
        self._enabled = False

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def get_state(self, key: str, default: Any = None) -> Any:
        """获取插件状态"""
        return self._state.get(key, default)

    def set_state(self, key: str, value: Any):
        """设置插件状态"""
        self._state[key] = value

    def __repr__(self):
        return f"<{self.__class__.__name__}(name={self.name}, enabled={self._enabled})>"


class _TrackingLifecycle(LifecycleManager):
    """生命周期包装器 — 追踪插件注册了哪些钩子。

    在插件注册期间替代原始 LifecycleManager 传入 plugin.on_register()，
    自动记录所有 register() 调用的 (hook, name) 对，
    供 PluginRegistry.unregister() 清理使用。
    """

    def __init__(self, lifecycle: LifecycleManager, tracker: list[tuple[LifecycleHook, str]]):
        super().__init__()
        self._lifecycle = lifecycle
        self._tracker = tracker

    def register(
        self,
        hook: LifecycleHook,
        func: Callable[..., Any],
        priority: int = 100,
        name: str | None = None,
        hook_type: str | None = None,
    ) -> None:
        resolved_name = name or getattr(func, "__name__", str(id(func)))
        self._tracker.append((hook, resolved_name))
        return self._lifecycle.register(hook, func, priority, name, hook_type)

    async def emit(
        self,
        hook: LifecycleHook,
        context: HookContext | None = None,
        **kwargs: Any,
    ) -> HookContext:
        return await self._lifecycle.emit(hook, context, **kwargs)


class PluginRegistry:
    """
    插件注册表
    管理所有插件的生命周期注册

    支持自动扫描目录加载插件——文件即开关：
      name.py             ✅ 加载（有效插件）
      name.py.disabled    ❌ 跳过（停用标记）
      _name.py            ❌ 跳过（内部模块）

    钩子清理：
      卸载插件时自动移除其注册的所有生命周期钩子，
      通过 _TrackingLifecycle 追踪每个插件的注册记录。
    """

    SKIP_FILES = {"base.py", "setup.py"}

    def __init__(self, lifecycle: LifecycleManager, plugin_dir: str | None = None):
        self._lifecycle = lifecycle
        self._plugins: dict[str, Plugin] = {}
        self._plugin_order: list[str] = []
        self._tracked_hooks: dict[str, list[tuple[LifecycleHook, str]]] = {}  # plugin_name → [(hook, name), ...]

        if plugin_dir is not None:
            self.scan(plugin_dir)

    def scan(self, plugin_dir: str) -> list[str]:
        """
        扫描目录下的插件文件，按命名约定自动加载并注册。

        返回已注册的插件名称列表。
        """
        # ── 清理旧扫描残留的 _plugin_scan_* 模块 ──
        # 热重载后 PluginRegistry._import_counter 归零，旧子模块
        # （如 _plugin_scan_1.plugin）残留在 sys.modules 中。
        # 不清除会导致目录插件 from .plugin import X 拿到旧类定义，
        # issubclass(旧类, 新Plugin基类) → False → 插件被静默跳过。
        stale_keys = [k for k in list(sys.modules) if k.startswith("_plugin_scan_")]
        for k in stale_keys:
            del sys.modules[k]

        if not os.path.isdir(plugin_dir):
            get_logger().info(f"[PluginRegistry] 目录不存在，跳过扫描: {plugin_dir}")
            return []

        registered: list[str] = []

        for entry in os.scandir(plugin_dir):
            fname = entry.name

            # ── 跳过隐藏/内部文件 ─────────────────
            if fname.startswith("_"):
                continue

            # ── 跳过已禁用标记（文件/目录统一处理） ─────
            if fname.endswith(".disabled"):
                continue

            # ── 目录插件（包） ────────────────────
            if entry.is_dir():
                init_path = os.path.join(entry.path, "__init__.py")
                if not os.path.isfile(init_path):
                    continue  # 没有 __init__.py 不是有效的包
                module = self._import_module(init_path)
                if module is None:
                    continue
                # 检查此包是否有 Plugin 子类
                found = self._register_plugin_from_module(module, entry.name)
                if found:
                    registered.append(entry.name)
                continue

            # ── 文件插件 ──────────────────────────
            # ── 过滤：只取 name.py（无多余后缀） ───
            if not fname.endswith(".py"):
                continue
            if fname in self.SKIP_FILES:
                continue

            # ── 动态导入 ──────────────────────────
            module = self._import_module(entry.path)
            if module is None:
                continue

            found = self._register_plugin_from_module(module, fname[:-3])
            if found:
                registered.append(fname[:-3])

        return registered

    # ── 内部：动态导入 ─────────────────────────────

    _import_counter = 0

    @classmethod
    def _import_module(cls, filepath: str) -> types.ModuleType | None:
        """从文件路径导入模块"""
        try:
            cls._import_counter += 1
            module_name = f"_plugin_scan_{cls._import_counter}"
            spec = importlib.util.spec_from_file_location(module_name, filepath)
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            return module
        except Exception as e:
            get_logger().info(f"[PluginRegistry] 加载模块失败 {filepath}: {e}")
            return None

    # ── 内部：从模块提取并注册 Plugin ─────────────

    def _register_plugin_from_module(self, module: types.ModuleType, fallback_name: str) -> bool:
        """从模块中查找 Plugin 子类并注册

        Returns:
            是否成功注册了插件
        """
        found = False
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj is Plugin:
                continue
            if not issubclass(obj, Plugin):
                continue
            # 允许 __init__.py 中 import 后 re-export 的类
            # 通过 module.__name__ starts with 判断，允许子模块

            # ── 自动实例化注册 ──────────────────
            try:
                instance = obj()
            except Exception as e:
                get_logger().info(f"[PluginRegistry] 实例化 {obj.__name__} 失败: {e}")
                continue

            if instance.name in self._plugins:
                # 已有同名插件 → 卸载旧的，用用户版本替换（警告：重名覆盖）
                old = self._plugins[instance.name]
                self.unregister(old.name)
                get_logger().warning(f"[PluginRegistry] ⚠️ 同名插件覆盖: {instance.name}")

            self._register_instance(instance)
            found = True

        return found

    # ── 内部：注册实例 ─────────────────────────────

    def _register_instance(self, plugin: Plugin):
        """注册插件实例（通过 _TrackingLifecycle 追踪钩子注册）"""
        self._plugins[plugin.name] = plugin
        self._plugin_order.append(plugin.name)
        plugin._lifecycle = self._lifecycle  # pyright: ignore[reportPrivateUsage]

        # 用追踪包装器替代原始 lifecycle，记录此插件注册的所有钩子
        tracker: list[tuple[LifecycleHook, str]] = []
        tracking = _TrackingLifecycle(self._lifecycle, tracker)
        plugin.on_register(tracking)
        if tracker:
            self._tracked_hooks[plugin.name] = tracker

        get_logger().info(f"[PluginRegistry] 自动注册: {plugin}")

    def register(self, plugin: Plugin) -> Plugin:
        """手动注册插件"""
        if plugin.name in self._plugins:
            raise ValueError(f"Plugin '{plugin.name}' already registered")

        self._register_instance(plugin)
        return plugin

    def unregister(self, name: str) -> Plugin | None:
        """注销插件（自动清理其注册的生命周期钩子）"""
        plugin = self._plugins.pop(name, None)
        if plugin:
            # 先清理此插件注册的所有钩子
            if name in self._tracked_hooks:
                for hook, hook_name in self._tracked_hooks.pop(name):
                    self._lifecycle.unregister(hook, hook_name)
            # 再调用插件的卸载钩子
            plugin.on_unregister()
            get_logger().info(f"[PluginRegistry] Unregistered: {plugin}")
        return plugin

    def get(self, name: str) -> Plugin | None:
        """获取插件"""
        return self._plugins.get(name)

    def list_plugins(self) -> list[str]:
        """列出所有已注册插件"""
        return self._plugin_order.copy()

    def __iter__(self):
        for name in self._plugin_order:
            yield self._plugins[name]


_P = TypeVar("_P", bound=Plugin)


def plugin(name: str, priority: int = 100) -> Callable[[type[_P]], type[_P]]:
    """插件装饰器"""

    def decorator(cls: type[_P]) -> type[_P]:
        cls.name = name
        # 将 priority 存入配置
        return cls

    return decorator
