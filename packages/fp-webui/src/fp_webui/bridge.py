"""
FP WebUI — lifecycle → EventBus 桥接插件（WebUIBridge）
==========================================

core 内置的 EventBridge 转发 LLM/工具级事件（llm_start/tool_call/…）；
本插件补上 **消息级** 转发，使「一切进入对话的消息都由 ws 事件驱动显示」：

  ON_CTX_APPEND(op="user") → {"type": "user_message", "content", "ts"}
      一切真正落入上下文的用户消息，单一咽喉点（agent.py 两处 append 都发此钩子）：
        - 直发：portal.run.send（/api/chat、空闲 ws 消息）
        - 环顶注入：空闲泵/submit 排队消费、ask_user 回答、后台任务终态、
          邻居消息（jobs.drain → add_user_message）
      前端仅凭该事件渲染用户气泡 → 多设备单端口看到同一份消息流，
      且与历史 REST 视图（落盘内容）天然一致——修复「泵注入消息在 live
      视图不显示、各设备互相错配」的问题。

  ON_COMMAND(handled 或 output 非空) → {"type": "user_message", "content",
                                        "ephemeral": true, "ts"}
      命令输入不落盘（不进上下文、不在历史文件），但发送端需要回显、
      其它设备需要同步 → 以 ephemeral 身份广播；前端渲染但不计入回溯索引
      （ephemeral 消息无 ↩ 回溯按钮，data-index 指向不存在的历史位置无意义）。
      显式让渡（handled=False 且无输出，稍后真正 append）**不发**，由
      ON_CTX_APPEND 兜底，防双渲染。

注册：随 ``portal.ctl.open(plugins=...)`` 走初始化前注册路径（main.py 两处 open）。

事件目录说明：EVENT_TYPES（core.events）冻结的是 EventBridge/IOChannel 事件；
与 done/cancelled/reload 等一样，user_message 属于前端传输层补充事件，
发布到同一总线，不改 core 目录。
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from fp_core.core.lifecycle import HookContext, LifecycleHook, LifecycleManager
from fp_core.plugins.base.plugin import Plugin


class WebUIBridge(Plugin):
    """WebUI 消息级生命周期桥（见模块 docstring）"""

    name = "webui_bridge"
    version = "1.0.0"

    def __init__(self, bus: Any):
        """Args:
        bus: fp_core.core.events.EventBus 实例（webui 侧为 portal.events 单例）
        """
        super().__init__()
        self._bus = bus

    async def _publish(self, payload: dict[str, Any]) -> None:
        """发布到总线；桥异常会 stop_propagation 打断主链，必须静默降级"""
        # 桥是旁路观察者，失败不影响对话主流程
        with contextlib.suppress(Exception):
            await self._bus.publish({"ts": time.time(), **payload})

    def on_register(self, lifecycle: LifecycleManager) -> None:
        lifecycle.register(
            LifecycleHook.ON_CTX_APPEND,
            self._on_ctx_append,
            priority=5,
            name="webui_ctx_append",
        )
        lifecycle.register(
            LifecycleHook.ON_COMMAND,
            self._on_command,
            priority=5,
            name="webui_command",
        )

    async def _on_ctx_append(
        self,
        ctx: HookContext,
        **kwargs: Any,
    ) -> None:
        """消息入库后：user 角色 → 广播 user_message（直发与环顶注入同此钩子）"""
        if kwargs.get("op") != "user":
            return
        message: Any = kwargs.get("message") or {}
        content = str(message.get("content") or "")
        if not content:
            return
        await self._publish({"type": "user_message", "content": content})

    async def _on_command(
        self,
        ctx: HookContext,
        **kwargs: Any,
    ) -> None:
        """命令执行完成：命令输入不落盘 → ephemeral 回显广播（全设备同步）。

        fallthrough（handled=False 且无输出）时输入随后真正 append，
        由 _on_ctx_append 发正式 user_message，此处必须让位防双渲染。
        """
        if not kwargs.get("handled") and not kwargs.get("output"):
            return
        # name 已含斜杠（agent.process: parts = user_input.strip().split(maxsplit=1)）
        name = str(kwargs.get("name") or "")
        if not name:
            return
        arg = str(kwargs.get("arg") or "")
        content = name + (f" {arg}" if arg else "")
        await self._publish({"type": "user_message", "content": content, "ephemeral": True})

    def on_unregister(self) -> None:
        """卸载钩子由 PluginRegistry 统一清理（见 unregister）"""
