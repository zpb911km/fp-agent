"""fp_core.api — 唯一接口组（外界与核心交流的唯一通信面）

协议文档：docs/dev/唯一接口组协议.md

前端（terminal/acp/webui 及未来任何前端）只允许导入本包 + 三个纯工具模块
（fp_core.logger / fp_core.config / fp_core.platform_utils）；
违规导入由 packages/fp-core/tests/test_api_surface.py 强制检查。

    from fp_core.api import portal

    portal.ctl.bootstrap()          # 入口第一行
    await portal.ctl.open(...)      # 创建实例
    ...  portal.run.*               # 实例内的操作
          portal.ctl.sessions.*     # 控制实例：会话管理
          portal.subscribe(...)     # 出向事件流
    await portal.ctl.close(...)     # 终结
"""

from fp_core.api.portal import ControlPlane, Portal, RunPlane, Subscription, portal
from fp_core.api.types import (
    ExitReason,
    InstanceNotOpenError,
    InstanceStatus,
    PortalError,
    ReloadDirective,
)
from fp_core.core.events import EVENT_TYPES, Event, EventBridge, EventBus
from fp_core.core.io import IOChannel, RestIO, WebSocketIO
from fp_core.core.lifecycle import HookContext, LifecycleHook, LifecycleManager
from fp_core.core.messages import Message, Response
from fp_core.core.session_ops import SessionInfo, SessionMeta
from fp_core.core.token_tracker import TokenUsage
from fp_core.plugins.base.plugin import Plugin

__all__ = [
    "EVENT_TYPES",
    "ControlPlane",
    "Event",
    "EventBridge",
    "EventBus",
    "ExitReason",
    "HookContext",
    "IOChannel",
    "InstanceNotOpenError",
    "InstanceStatus",
    "LifecycleHook",
    "LifecycleManager",
    "Message",
    "Plugin",
    "Portal",
    "PortalError",
    "ReloadDirective",
    "Response",
    "RestIO",
    "RunPlane",
    "SessionInfo",
    "SessionMeta",
    "Subscription",
    "TokenUsage",
    "WebSocketIO",
    "portal",
]
