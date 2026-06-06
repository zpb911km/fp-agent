"""
插件基类
所有功能插件都继承自此基类
"""

from typing import Optional, Dict, Any, List
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from core.lifecycle import LifecycleHook, LifecycleManager


@dataclass
class PluginConfig:
    """插件配置基类"""
    enabled: bool = True
    priority: int = 100
    metadata: Dict[str, Any] = field(default_factory=dict)


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
    
    def __init__(self, config: Optional[PluginConfig] = None):
        self.config = config or PluginConfig()
        self._lifecycle: Optional[LifecycleManager] = None
        self._enabled = self.config.enabled
        self._state: Dict[str, Any] = {}
    
    @abstractmethod
    def on_register(self, lifecycle: LifecycleManager):
        """
        注册插件钩子
        子类实现此方法来注册自己的生命周期钩子
        """
        pass
    
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


class PluginRegistry:
    """
    插件注册表
    管理所有插件的生命周期注册
    """
    
    def __init__(self, lifecycle: LifecycleManager):
        self._lifecycle = lifecycle
        self._plugins: Dict[str, Plugin] = {}
        self._plugin_order: List[str] = []
    
    def register(self, plugin: Plugin) -> Plugin:
        """注册插件"""
        if plugin.name in self._plugins:
            raise ValueError(f"Plugin '{plugin.name}' already registered")
        
        self._plugins[plugin.name] = plugin
        self._plugin_order.append(plugin.name)
        
        # 调用插件的注册回调
        plugin._lifecycle = self._lifecycle
        plugin.on_register(self._lifecycle)
        
        print(f"[PluginRegistry] Registered: {plugin}")
        return plugin
    
    def unregister(self, name: str) -> Optional[Plugin]:
        """注销插件"""
        plugin = self._plugins.pop(name, None)
        if plugin:
            plugin.on_unregister()
            print(f"[PluginRegistry] Unregistered: {plugin}")
        return plugin
    
    def get(self, name: str) -> Optional[Plugin]:
        """获取插件"""
        return self._plugins.get(name)
    
    def list_plugins(self) -> List[str]:
        """列出所有已注册插件"""
        return self._plugin_order.copy()
    
    def __iter__(self):
        for name in self._plugin_order:
            yield self._plugins[name]


def plugin(name: str, priority: int = 100):
    """插件装饰器"""
    def decorator(cls):
        cls.name = name
        # 将 priority 存入配置
        return cls
    return decorator