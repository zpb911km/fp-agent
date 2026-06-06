"""
通知插件
监听生命周期钩子，提供桌面通知和声音提醒
"""

import os
import subprocess
import time
from typing import Optional, Dict, List, Any, Set

from plugins.base.plugin import Plugin, PluginConfig
from core.lifecycle import LifecycleHook, HookContext, LifecycleManager

# ── 可用的系统提示音 ──────────────────────────────────
SOUND_DIR = "/usr/share/sounds/freedesktop/stereo"
DEFAULT_SOUNDS: Dict[str, str] = {
    "message":  os.path.join(SOUND_DIR, "message-new-instant.oga"),
    "info":     os.path.join(SOUND_DIR, "dialog-information.oga"),
    "warning":  os.path.join(SOUND_DIR, "dialog-warning.oga"),
    "error":    os.path.join(SOUND_DIR, "dialog-error.oga"),
    "complete": os.path.join(SOUND_DIR, "complete.oga"),
    "bell":     os.path.join(SOUND_DIR, "bell.oga"),
    "service":  os.path.join(SOUND_DIR, "service-login.oga"),
}
# 过滤掉不存在的文件
DEFAULT_SOUNDS = {k: v for k, v in DEFAULT_SOUNDS.items() if os.path.exists(v)}


# ── 钩子 → 通知类型映射 ──────────────────────────────
HOOK_NOTIFICATION_MAP = {
    # LifecycleHook.ON_MESSAGE_RECEIVED:  ("message", "💬 用户消息"),
    # LifecycleHook.ON_BEFORE_LLM_CALL:   ("info",    "🤔 思考中..."),
    # LifecycleHook.ON_AFTER_LLM_CALL:    ("info",    "✅ 思考完成"),
    # LifecycleHook.ON_TOOL_SELECT:       ("info",    "🛠️ 选择工具"),
    # LifecycleHook.ON_TOOL_CALL:         ("info",    "⚡ 执行工具"),
    # LifecycleHook.ON_TOOL_RESULT:       ("complete","✅ 工具完成"),
    LifecycleHook.ON_ERROR:             ("error",   "❌ 发生错误"),
    LifecycleHook.ON_BEFORE_RESPONSE:   ("complete","💡 回复完成"),
    # LifecycleHook.ON_CONTEXT_UPDATE:    ("info",    "📦 上下文更新"),
    LifecycleHook.ON_SHUTDOWN:          ("service", "🛑 程序关闭"),
}


class NotificationPlugin(Plugin):
    """
    通知插件
    
    监听关键生命周期事件，触发桌面通知和/或声音提醒。
    所有配置都在类内部以实例属性呈现，不依赖外部元数据。
    
    标志位（可在创建时传参覆盖）:
      desktop: bool           桌面通知开关（默认 True）
      sound: bool             声音提醒开关（默认 False）
      rate_limit: float       最低通知间隔秒数（默认 2.0）
      hooks: List[str]        监听的钩子名称列表（空=全部）
      max_desktop_len: int    通知正文最大长度（默认 120）
    """
    
    name = "notification"
    version = "1.0.1"
    
    # ── 默认标志位（类级别） ──────────────────────────
    desktop: bool = True
    sound: bool = True
    rate_limit: float = 2.0
    hooks: List[str] = []
    max_desktop_len: int = 120
    
    def __init__(
        self,
        config: Optional[PluginConfig] = None,
        # 标志位参数直接提升到构造器
        desktop: Optional[bool] = None,
        sound: Optional[bool] = None,
        rate_limit: Optional[float] = None,
        hooks: Optional[List[str]] = None,
        max_desktop_len: Optional[int] = None,
    ):
        # 如果未传 config，用默认值构造
        if config is None:
            config = PluginConfig(enabled=True, priority=50)
        super().__init__(config)
        
        # 从传参覆盖标志位
        if desktop is not None:
            self.desktop = desktop
        if sound is not None:
            self.sound = sound
        if rate_limit is not None:
            self.rate_limit = rate_limit
        if hooks is not None:
            self.hooks = hooks
        if max_desktop_len is not None:
            self.max_desktop_len = max_desktop_len
        
        self._last_notify_time: float = 0
        self._active_hooks: Set[LifecycleHook] = set()
    
    # ── 注册 ───────────────────────────────────────────
    
    def on_register(self, lifecycle: LifecycleManager):
        """注册生命周期钩子"""
        hooks_to_listen: List[LifecycleHook] = []
        if self.hooks:
            for name in self.hooks:
                try:
                    hooks_to_listen.append(LifecycleHook[name])
                except KeyError:
                    print(f"[Notification] 未知钩子: {name}")
        else:
            hooks_to_listen = list(HOOK_NOTIFICATION_MAP.keys())
        
        self._active_hooks = set(hooks_to_listen)
        
        for hook in hooks_to_listen:
            lifecycle.register(hook, self._make_handler(hook), priority=50, name=f"notify_{hook.name}")
    
    # ── 内部 ───────────────────────────────────────────
    
    def _make_handler(self, hook: LifecycleHook):
        """为指定钩子生成处理函数"""
        async def handler(ctx: HookContext, **kwargs):
            if not self._enabled:
                return
            self._notify(hook, **kwargs)
        return handler
    
    def _notify(self, hook: LifecycleHook, **kwargs):
        """执行通知"""
        # 速率限制
        now = time.time()
        if now - self._last_notify_time < self.rate_limit:
            return
        self._last_notify_time = now
        
        entry = HOOK_NOTIFICATION_MAP.get(hook)
        if not entry:
            return
        sound_key, default_title = entry
        
        title = default_title
        body = self._build_body(hook, **kwargs)
        
        if self.desktop:
            self._desktop_notify(title, body)
        
        if self.sound and sound_key in DEFAULT_SOUNDS:
            self._play_sound(DEFAULT_SOUNDS[sound_key])
    
    def _build_body(self, hook: LifecycleHook, **kwargs) -> str:
        """根据钩子和 kwargs 构建通知正文"""
        max_len = self.max_desktop_len
        
        if hook == LifecycleHook.ON_MESSAGE_RECEIVED:
            content = kwargs.get("content", "")
            return (content[:max_len] + "...") if len(content) > max_len else content
        
        elif hook == LifecycleHook.ON_TOOL_CALL:
            tool_name = kwargs.get("tool_name", "?")
            return f"工具: {tool_name}"
        
        elif hook == LifecycleHook.ON_TOOL_RESULT:
            tool_name = kwargs.get("tool_name", "?")
            result = kwargs.get("result", "")
            brief = (result[:60] + "...") if len(result) > 60 else result
            return f"{tool_name}: {brief}"
        
        elif hook == LifecycleHook.ON_ERROR:
            error = kwargs.get("error", "未知错误")
            return (error[:max_len] + "...") if len(error) > max_len else error
        
        elif hook == LifecycleHook.ON_BEFORE_RESPONSE:
            content = kwargs.get("content", "")
            return (content[:max_len] + "...") if len(content) > max_len else content
        
        elif hook == LifecycleHook.ON_AFTER_LLM_CALL:
            has_tc = kwargs.get("has_tool_calls", False)
            tool_names = kwargs.get("tool_names", [])
            if has_tc and tool_names:
                return f"将调用: {', '.join(tool_names)}"
            return ""
        
        elif hook == LifecycleHook.ON_TOOL_SELECT:
            tools = kwargs.get("tools", [])
            return f"计划调用 {len(tools)} 个工具: {', '.join(tools[:3])}"
        
        elif hook == LifecycleHook.ON_CONTEXT_UPDATE:
            msg_count = kwargs.get("msg_count", 0)
            return f"当前上下文 {msg_count} 条消息"
        
        return ""
    
    @staticmethod
    def _desktop_notify(title: str, body: str):
        """发送桌面通知"""
        if not title and not body:
            return
        try:
            subprocess.run(
                ["notify-send", "-a", "Five Pebbles", title, body],
                timeout=1,
                capture_output=True,
            )
        except Exception:
            pass
    
    @staticmethod
    def _play_sound(sound_file: str):
        """播放提示音（异步派生子进程）"""
        try:
            subprocess.Popen(
                ["paplay", sound_file],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


# ── 快捷创建 ──────────────────────────────────────────

def create_notification_plugin(
    desktop: bool = True,
    sound: bool = True,
    rate_limit: float = 2.0,
    hooks: Optional[List[str]] = None,
    max_desktop_len: int = 120,
) -> NotificationPlugin:
    """
    创建通知插件实例
    
    所有标志位直接映射到插件类属性，不绕道外部元数据。
    用法:
      agent.plugins.register(create_notification_plugin())
      agent.plugins.register(create_notification_plugin(sound=False, rate_limit=1.0))
    """
    return NotificationPlugin(
        desktop=desktop,
        sound=sound,
        rate_limit=rate_limit,
        hooks=hooks or [],
        max_desktop_len=max_desktop_len,
    )
