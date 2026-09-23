"""reload handoff — 跨进程续接契约（机制层，入口无关）

流程（由 reload 插件在旧实例中发起，见用户红线 reload_gate_protocol）：

    旧实例: 补齐本轮全部缺失 tool 结果 → save_context 落盘 → 写 handoff 文件
            → 设置 FP_RELOAD_HANDOFF/FP_RELOAD_SID 环境变量 → os.execve 按原启动命令重启
    新实例: 入口构造 Agent 时传 resume=FP_RELOAD_SID 恢复会话
            → Agent 就绪后调 consume_reload_handoff() 校验并置 state._pending_continue
            → 入口在自己的控制点 await agent.continue_conversation(io=本入口IO)
            → 渲染 Response，最后 state._pending_continue = None

**新入口接入契约（三步，未来新入口照此实现即自动兼容 reload）：**
  1. 构造 Agent：``resume=os.environ.get("FP_RELOAD_SID")``
  2. Agent 就绪（ensure_initialized 后）：``consume_reload_handoff(agent)``
  3. 返回 True 则驱动 ``await agent.continue_conversation(io=...)``（用本入口的 IO），
     渲染返回内容，完成后把 ``agent.state._pending_continue`` 置 None。

handoff 文件：``{DATA}/reload_handoff.json`` = ``{sid, ts, frontend}``。
消费即删（防同进程二次消费），TTL 兜底（防陈旧 handoff 误触发）。
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fp_core.core.agent import Agent

# handoff 有效期（秒）。reload 是落盘→exec 的即时衔接，超过此值视为陈旧残留。
HANDOFF_TTL = 900


def handoff_path() -> str:
    """handoff 文件的规范路径（reload 插件与本模块共用）"""
    from fp_core.platform_utils import get_data_dir

    return os.path.join(get_data_dir(), "reload_handoff.json")


def consume_reload_handoff(agent: Agent) -> bool:
    """校验并消费 reload handoff，成功则置 agent.state._pending_continue。

    幂等且一次性：无论成败都会清除环境变量 FP_RELOAD_HANDOFF/FP_RELOAD_SID
    并删除 handoff 文件，保证同一进程不会二次触发续接。

    Returns:
        True = 存在有效 handoff（state._pending_continue 已置位），
        False = 无 handoff / 已过期 / sid 不匹配（resume 未生效）等异常情况。
    """
    path = os.environ.get("FP_RELOAD_HANDOFF", "")
    os.environ.pop("FP_RELOAD_HANDOFF", None)
    os.environ.pop("FP_RELOAD_SID", None)
    if not path:
        return False

    payload: dict[str, Any] | None = None
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        payload = None
    finally:
        # 消费即删：文件是单次通行证
        with contextlib.suppress(OSError):
            os.remove(path)

    if not isinstance(payload, dict):
        return False

    # TTL：exec 是即时衔接，900 秒还没消费说明是陈旧残留
    if time.time() - float(payload.get("ts", 0)) > HANDOFF_TTL:
        return False

    # sid 必须与当前实例恢复到的会话一致——否则 resume 没生效，续接会张冠李戴
    if payload.get("sid") != agent.session.session_id:
        return False

    agent.state._pending_continue = payload  # pyright: ignore[reportPrivateUsage] 设计内跨类协议（state.py 注释明确该字段供 handoff 流程使用）
    return True
