"""
生命周期管理系统
基于事件驱动，支持同步/异步钩子
"""

from typing import Callable, Dict, List, Any, Optional
from dataclasses import dataclass, field
from enum import Enum, auto
import asyncio
from functools import wraps
import time


class LifecycleHook(Enum):
    """生命周期钩子枚举"""
    # 初始化阶段
    ON_INIT = auto()           # Agent初始化
    ON_CONFIG_LOADED = auto()  # 配置加载完成
    
    # 消息处理阶段
    ON_MESSAGE_RECEIVED = auto()   # 收到消息
    ON_MESSAGE_PARSE = auto()     # 消息解析
    ON_MESSAGE_FILTER = auto()    # 消息过滤
    
    # 执行阶段
    ON_BEFORE_THINK = auto()      # 思考前
    ON_THINK = auto()             # 思考中
    ON_AFTER_THINK = auto()       # 思考后
    
    # LLM交互阶段
    ON_BEFORE_LLM_CALL = auto()   # LLM调用前
    ON_LLM_CALL = auto()          # LLM调用中
    ON_AFTER_LLM_CALL = auto()    # LLM调用后
    
    # 响应阶段
    ON_BEFORE_RESPONSE = auto()   # 生成响应前
    ON_RESPONSE = auto()          # 生成响应
    ON_AFTER_RESPONSE = auto()    # 生成响应后
    
    # 工具执行阶段
    ON_TOOL_SELECT = auto()       # 工具选择
    ON_TOOL_CALL = auto()         # 工具调用前
    ON_TOOL_RESULT = auto()       # 工具调用后
    ON_TOOL_ERROR = auto()        # 工具错误
    
    # 上下文管理
    ON_CONTEXT_UPDATE = auto()    # 上下文更新
    ON_CONTEXT_READ = auto()      # 上下文读取
    
    # 错误处理
    ON_ERROR = auto()             # 发生错误
    ON_RETRY = auto()             # 重试
    
    # 资源管理
    ON_SHUTDOWN = auto()          # 关闭
    ON_CLEANUP = auto()           # 清理资源


@dataclass
class HookContext:
    """钩子执行上下文"""
    hook: LifecycleHook
    data: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    stop_propagation: bool = False
    error: Optional[Exception] = None


class LifecycleManager:
    """
    生命周期管理器
    支持同步/异步钩子，按优先级执行
    """
    
    def __init__(self, enable_log: bool = False):
        self._hooks: Dict[LifecycleHook, List[tuple]] = {}  # hook -> [(priority, name, func)]
        self._enable_log = enable_log
        self._stats: Dict[str, int] = {}
    
    def register(
        self,
        hook: LifecycleHook,
        func: Callable,
        priority: int = 100,
        name: Optional[str] = None
    ):
        """注册钩子函数"""
        if hook not in self._hooks:
            self._hooks[hook] = []
        
        name = name or getattr(func, '__name__', str(id(func)))
        
        # 按优先级插入（小的先执行）
        hooks_list = self._hooks[hook]
        inserted = False
        for i, (p, n, _) in enumerate(hooks_list):
            if priority < p:
                hooks_list.insert(i, (priority, name, func))
                inserted = True
                break
        if not inserted:
            hooks_list.append((priority, name, func))
        
        if self._enable_log:
            print(f"[Lifecycle] Registered '{name}' on {hook.name} (priority={priority})")
    
    def unregister(self, hook: LifecycleHook, name: str) -> bool:
        """注销钩子"""
        if hook in self._hooks:
            for i, (p, n, f) in enumerate(self._hooks[hook]):
                if n == name:
                    self._hooks[hook].pop(i)
                    return True
        return False
    
    async def emit(
        self,
        hook: LifecycleHook,
        context: Optional[HookContext] = None,
        **kwargs
    ) -> HookContext:
        """
        触发钩子
        返回最终的上下文（可能被修改）
        """
        if context is None:
            context = HookContext(hook=hook)
        
        # 直接合并 kwargs 到 context.data
        for key, value in kwargs.items():
            if key == "data":
                context.data.update(value)
            else:
                context.data[key] = value
        
        if self._enable_log:
            print(f"[Lifecycle] Emitting {hook.name}...")
        
        if hook not in self._hooks or not self._hooks[hook]:
            return context
        
        for priority, name, func in self._hooks[hook]:
            if context.stop_propagation:
                break
            
            try:
                start = time.time()
                if asyncio.iscoroutinefunction(func):
                    result = await func(context, **context.data)
                else:
                    result = func(context, **context.data)
                
                elapsed = time.time() - start
                self._stats[f"{hook.name}:{name}"] = self._stats.get(f"{hook.name}:{name}", 0) + 1
                
                if self._enable_log:
                    print(f"[Lifecycle]   -> {name} took {elapsed*1000:.2f}ms")
                
                # 如果钩子返回新上下文，合并
                if result is not None and isinstance(result, HookContext):
                    context = result
                    
            except Exception as e:
                context.error = e
                context.stop_propagation = True
                if self._enable_log:
                    print(f"[Lifecycle]   -> {name} ERROR: {e}")
        
        return context
    
    def emit_sync(self, hook: LifecycleHook, context: Optional[HookContext] = None, **kwargs) -> HookContext:
        """同步触发钩子（用于非异步环境）"""
        if context is None:
            context = HookContext(hook=hook)
        
        # 直接合并 kwargs 到 context.data
        for key, value in kwargs.items():
            if key == "data":
                context.data.update(value)
            else:
                context.data[key] = value
        
        if hook not in self._hooks or not self._hooks[hook]:
            return context
        
        for priority, name, func in self._hooks[hook]:
            if context.stop_propagation:
                break
            try:
                result = func(context, **context.data)
                if result is not None and isinstance(result, HookContext):
                    context = result
            except Exception as e:
                context.error = e
                context.stop_propagation = True
        
        return context
    
    def get_hooks(self, hook: LifecycleHook) -> List[str]:
        """获取已注册的所有钩子名称"""
        return [name for _, name, _ in self._hooks.get(hook, [])]
    
    def clear(self, hook: Optional[LifecycleHook] = None):
        """清除钩子"""
        if hook:
            self._hooks[hook] = []
        else:
            self._hooks.clear()
    
    def get_stats(self) -> Dict[str, int]:
        """获取钩子执行统计"""
        return self._stats.copy()


def hook(hook_type: LifecycleHook, priority: int = 100):
    """装饰器：注册生命周期钩子"""
    def decorator(func):
        func._lifecycle_hook = hook_type
        func._lifecycle_priority = priority
        @wraps(func)
        async def async_wrapper(context, **kwargs):
            return await func(context, **kwargs)
        @wraps(func)
        def sync_wrapper(context, **kwargs):
            return func(context, **kwargs)
        
        # 根据原函数类型返回包装器
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper
    return decorator