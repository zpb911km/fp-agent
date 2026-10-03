"""fp_core.api.types — 唯一接口组的协议数据类型

这些类型是「外界 ↔ 核心」跨边界传递的**全部**数据形态；
前端不得触碰 state/conversation/session 内部，越界只传这里的协议对象。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeAlias

from fp_core.core.session_ops import SessionInfo, SessionMeta

__all__ = [
    "EVENT_TYPES",
    "Event",
    "ExitReason",
    "InstanceNotOpenError",
    "InstanceStatus",
    "PortalError",
    "ReloadDirective",
    "SessionInfo",
    "SessionMeta",
]


class ExitReason(Enum):
    """关闭实例的原因（取代原 silent_shutdown / nuclear_exit 散落布尔）

    FINAL   — 正常终结：保存上下文 + 摘要 + 退出面板/提示
    RECYCLE — 热切换：保存上下文 + 摘要，静默（无面板无提示）。reload/重建用。
    DISCARD — 不留痕：不保存，删除当前会话文件，静默。核弹退出用。
    """

    FINAL = "final"
    RECYCLE = "recycle"
    DISCARD = "discard"


@dataclass(frozen=True)
class ReloadDirective:
    """ctl.take_reload() 的结构化结果（原三份前端 handoff 样板的收编形态）

    notice          — kind=command 的完成提示行（前端自行渲染），None = 无
    should_continue — kind=tool 的有效续接，True 时前端应驱动
                      `await portal.run.continue_(io=...)`
    """

    notice: str | None = None
    should_continue: bool = False


@dataclass(frozen=True)
class InstanceStatus:
    """run.status 的只读实例快照（替代前端对 agent.state 的散落读取）"""

    model: str
    session_id: str
    is_processing: bool
    cancelled: bool


# 事件形状：{"type": str, "seq": int, "ts": float, **payload} —— 目录见 core.events.EVENT_TYPES
Event: TypeAlias = dict[str, Any]

# 协议级事件类型目录的 re-export（冻结，新增只做加法）
from fp_core.core.events import EVENT_TYPES  # noqa: E402  (类型与目录同源)


class PortalError(Exception):
    """唯一接口组的基础异常"""


class InstanceNotOpenError(PortalError):
    """实例内操作（run.*）在实例未开启时调用"""

    def __init__(self) -> None:
        super().__init__("实例未开启：请先调用 portal.ctl.open()")
