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

  连续性校验 → {"type": "history_changed", "msg_count", "ts"}
      改写型命令（/back、/sc、/clear、/fork…）与改写型**工具**（shortcircuit
      退化/合并历史块）都走 ``ConversationState.set_messages`` 整体替换上下文，
      不经 append → 前端卡片与后端落盘失配，必须让前端 clearUI + 重拉历史。

      判据不是「命令名」也不是「长度前后对比」，而是 **msg_count 连续性**：
      所有入库都经 add_* → agent 每条都发 ON_CTX_APPEND(msg_count=入库后真实
      长度)，桥因此可以维护「期望值」并一路跟随。任何绕过 append 的批量改写
      都会让下一条消息的 msg_count ≠ expected+1（或在无 append 的观察点上
      ≠ expected）——即「连续性断裂」= 发生过重写。

        观察点（全部为**已有**钩子，不新增事件、不改 core）：
          ON_CTX_APPEND     期望 expected+1（每条入库必经）
          ON_ITERATION      期望 expected  （工具轮完成，覆盖「改完不 append」）
          ON_TURN_END       期望 expected  （终答）
          ON_CONTEXT_UPDATE 期望 expected  （保存前，覆盖本轮末重写）
          ON_COMMAND        期望 expected  （命令路径，len(messages)）

      失效模式刻意选「响亮误报」而非「静默漏报」：将来若有 append 点漏发
      ON_CTX_APPEND，后续会多报几次 history_changed（前端多刷一次，无害）；
      反之漏判会让前端长期显示失配卡片。

      已知漏判：重写后净增恰好 1 条消息（长度恰为 expected+1），或 replace_all
      换成等长内容。两者均需极端巧合，且切 session 时前端本就会重拉。

      时机纪律：turn 进行中检测到的重写**不能立刻刷**——此刻前端正在流式渲染
      当前卡片，clearUI 会把它抹掉，而落盘历史尚不含本轮，重拉必失配。
      桥照常发布（多设备同步靠它），由前端 ``_pendingHistoryRefresh`` 记账，
      待 done/cancelled/error 收尾后统一重拉。

注册：随 ``portal.ctl.open(plugins=...)`` 走初始化前注册路径（main.py 两处 open）。

事件目录说明：EVENT_TYPES（core.events）冻结的是 EventBridge/IOChannel 事件；
与 done/cancelled/reload 等一样，user_message 属于前端传输层补充事件，
发布到同一总线，不改 core 目录。
"""

from __future__ import annotations

import contextlib
import time
from typing import Any, cast

# 协议 §5：前端只许 fp_core.api（Plugin/HookContext/LifecycleHook 均由其导出）
from fp_core.api import HookContext, LifecycleHook, LifecycleManager, Plugin


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
        # 连续性校验基线：最近一次确认的「真实消息数」。
        # None = 尚未立基线（启动/reload 后首个观察点只登记、不判定）
        self._expected: int | None = None

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
        # ── 连续性校验的无 append 观察点（覆盖「改完不 append」的重写）──
        # 三个钩子载荷均自带 msg_count，无需 messages 快照
        for hook, handler, hname in (
            (LifecycleHook.ON_ITERATION, self._on_iteration, "webui_iteration"),
            (LifecycleHook.ON_TURN_END, self._on_turn_end, "webui_turn_end"),
            (LifecycleHook.ON_CONTEXT_UPDATE, self._on_context_update, "webui_ctx_update"),
        ):
            lifecycle.register(hook, handler, priority=5, name=hname)
        # ── 基线同步点：引擎内部的一致性修复，不触发前端刷新 ──
        lifecycle.register(
            LifecycleHook.ON_CTX_REPAIR,
            self._on_ctx_repair,
            priority=5,
            name="webui_ctx_repair",
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
        """消息入库后：连续性校验（所有 op）+ user 角色 → 广播 user_message"""
        # 校验必须跑在所有 op 上（tool/assistant 同样是「单条入库」基准点），
        # 不能放在 op=="user" 的早退之后。
        # 优先取载荷里的 msg_count（agent 计算的权威长度），缺失才回退 messages 快照长度
        count = self._as_count(kwargs.get("msg_count"))
        if count is None:
            count = self._count_messages(kwargs.get("messages"))
        await self._check_continuity(count, is_append=True)
        # ── user 角色 → 气泡广播（直发与环顶注入同此钩子）──
        if kwargs.get("op") != "user":
            return
        message: Any = kwargs.get("message") or {}
        content = str(message.get("content") or "")
        if not content:
            return
        await self._publish({"type": "user_message", "content": content})

    async def _check_continuity(self, count: int | None, *, is_append: bool) -> None:
        """msg_count 连续性校验：断裂 = 上下文被绕过 append 重写（见模块 docstring）

        Args:
            count: 本次观察点看到的真实消息数（None = 载荷缺失，无法校验，跳过）
            is_append: True 表示该观察点是「单条入库」，正常应为 expected+1；
                 False 表示该观察点自身不改变长度，正常应等于 expected
        """
        if count is None:
            return
        prev = self._expected
        self._expected = count  # 无论如何跟住真实值，避免一次断裂引发连环误报
        if prev is None:
            return  # 首个观察点（启动/reload 后）：只立基线，不判定
        expected = prev + 1 if is_append else prev
        if count == expected:
            return
        # 断裂 → 上下文被整体重写，前端卡片与落盘失配
        await self._publish({"type": "history_changed", "msg_count": count})

    @staticmethod
    def _as_count(raw: Any) -> int | None:
        """钩子 data.msg_count 载荷 → int（缺失/非整数返回 None）"""
        return raw if isinstance(raw, int) else None

    async def _on_iteration(self, ctx: HookContext, **kwargs: Any) -> None:
        """工具轮完成回环顶：覆盖「工具改完上下文却不 append」的重写"""
        await self._check_continuity(self._as_count(kwargs.get("msg_count")), is_append=False)

    async def _on_turn_end(self, ctx: HookContext, **kwargs: Any) -> None:
        """终答（无 tool_calls）：本轮结束前最后一道连续性闸门"""
        await self._check_continuity(self._as_count(kwargs.get("msg_count")), is_append=False)

    async def _on_context_update(self, ctx: HookContext, **kwargs: Any) -> None:
        """上下文已更新（落盘前）：覆盖本轮末发生的重写"""
        await self._check_continuity(self._as_count(kwargs.get("msg_count")), is_append=False)

    async def _on_ctx_repair(self, ctx: HookContext, **kwargs: Any) -> None:
        """tool ordering 修复：只同步基线，**不**触发前端刷新

        repair 把孤立 tool 消息合并为一条 system 消息（「不丢弃信息，转为合成
        消息保留」），长度可能变短 —— 属引擎内部一致性修复，而非用户可见的
        删除。若按重写处理会让前端 clearUI，抹掉刚显示的中断提示，且修复后的
        卡片与落盘并无实质差异。同步基线即可，避免后续 append 被连锁误判。
        """
        count = self._count_messages(kwargs.get("messages"))
        if count is not None:
            self._expected = count

    @staticmethod
    def _count_messages(raw: Any) -> int | None:
        """钩子 data.messages 载荷 → 消息数（非 list 载荷返回 None）

        pyright：isinstance 收窄 Any 会得到 list[Unknown]，len() 触发
        reportUnknownArgumentType → cast 为 list[Any] 后再取长度。
        """
        if not isinstance(raw, list):
            return None
        return len(cast("list[Any]", raw))

    async def _on_command(
        self,
        ctx: HookContext,
        **kwargs: Any,
    ) -> None:
        """命令执行完成：连续性校验 + ephemeral 回显广播（全设备同步）。

        fallthrough（handled=False 且无输出）时输入随后真正 append，
        由 _on_ctx_append 发正式 user_message，此处必须让位防双渲染。
        """
        # ── 连续性校验：命令（/back /sc /clear /fork…）set_messages 重写上下文 ──
        # 命令名不硬编码、也不需要前后快照：命令不改长度时 count==expected 即通过
        await self._check_continuity(self._count_messages(kwargs.get("messages")), is_append=False)
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
