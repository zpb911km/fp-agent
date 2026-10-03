"""ZetaPlugin — 对等协作协议插件。

在实例内提供**邻居通信面**：发现（发现即告知 LLM）、铃声（不打断只提示）、
``peer_list`` / ``peer_send`` / ``peer_answer`` 工具。领域模型见
:mod:`fp_core.plugins.zeta.domain`（协议无关 IR）。

形态（见 docs/dev/zeta_协议总览.md）：
    - in-process L2 插件，合法引用 L1（``jobs.inject_event`` + ``LifecycleHook``）；
    - 用户启动任何前端即拉起本实例的 Zeta；
    - **不穿透 portal 改 core**——用的是 core 预留扩展点；
    - 一目录一实例（``cwd`` 即身份键），锁冲突则本实例不启用邻居面。

测试/子 agent 环境经 ``FP_ZETA_DISABLE=1`` / ``FP_IS_SUBAGENT=1`` 关闭网络面。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from typing import Any, cast

from fp_core.core import jobs
from fp_core.core.lifecycle import HookContext, LifecycleHook, LifecycleManager
from fp_core.core.session import get_current_session_id
from fp_core.logger import get_logger
from fp_core.plugins.base.plugin import Plugin, PluginConfig
from fp_core.plugins.zeta.contracts import ContractExchange
from fp_core.plugins.zeta.discovery import (
    InstanceConflictError,
    InstanceRegistry,
    SharedDirBackend,
    build_card,
    check_session_workspace,
    derive_instance_name,
    infer_business,
    refresh_heartbeat,
)
from fp_core.plugins.zeta.domain import NeighborCard, neighbor_brief
from fp_core.plugins.zeta.peer import PeerService
from fp_core.plugins.zeta.transport import DirTransport
from fp_core.tools import ToolRegistry
from fp_core.tools.core import OpenAISchema

logger = get_logger()

ZETA_DESCRIPTION = (
    "【邻居协作】多个 FP 实例像邻居一样协作（无上下级、可离线留话）："
    "peer_list 查看在线邻居名片（谁在线、干什么）、peer_send 主动联系某邻居"
    "（给 to/topic/body）、收到来电会以【铃声】提示，用 peer_answer 接听。"
    "契约协商以 hash+schema 双锁判定，只有机器校验通过才算谈成。"
)

PEER_LIST_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "peer_list",
        "description": (
            "列出**当前**在线邻居实例的名片（cwd/business/tags/status/心跳年龄/current_task）。"
            "邻居状态随时间变化——每次需要寻址或联系前请**重新调用**，不要凭上下文里的旧结果作答。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

PEER_SEND_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "peer_send",
        "description": "主动联系一个邻居实例（投递一条消息；对方离线则留话）。",
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "邻居名（见 peer_list）"},
                "topic": {"type": "string", "description": "话题大类，如 contract/task/chat"},
                "body": {"type": "string", "description": "正文"},
            },
            "required": ["to", "body"],
        },
    },
}

PEER_ANSWER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "peer_answer",
        "description": "接听一通邻居来电，取回正文（省略 call_id 则接最早一通）。",
        "parameters": {
            "type": "object",
            "properties": {"call_id": {"type": "string", "description": "可选，来电 id"}},
        },
    },
}

PEER_PROPOSE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "peer_propose",
        "description": (
            "向邻居提议一份协作契约（fp-contract 结构：parties/scope/interfaces 等）。"
            "本地机器校验通过才发出；对方同样机器校验，双方 hash 一致且无 issues 才算谈成。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "邻居名（见 peer_list）"},
                "contract": {
                    "type": "object",
                    "description": "契约 JSON 对象（contract_version/id/name/parties/scope/interfaces/governance…）",
                },
            },
            "required": ["to", "contract"],
        },
    },
}

PEER_ACK_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "peer_ack",
        "description": "对邻居的契约提议回执（accept=true 表示已机器校验通过并同意）。",
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "邻居名"},
                "contract_id": {"type": "string", "description": "契约 id"},
                "hash": {"type": "string", "description": "接听时机器校验给出的 hash（锁字节）"},
                "accept": {"type": "boolean", "description": "是否接受"},
            },
            "required": ["to", "contract_id", "hash"],
        },
    },
}


class ZetaPlugin(Plugin):
    name = "zeta"
    version = "0.2.0"

    def __init__(self, config: PluginConfig | None = None):
        super().__init__(config)
        self._registry: ToolRegistry | None = None
        self._registered: list[str] = []
        self._backend: SharedDirBackend | None = None
        self._card: NeighborCard | None = None
        self._service: PeerService | None = None
        self._instances: InstanceRegistry | None = None
        self._workspace_mismatch: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._poll_interval = 2.0
        self._disabled_reason: str | None = None

    # ── 生命周期 ─────────────────────────────────────

    def on_register(self, lifecycle: LifecycleManager):
        if os.environ.get("FP_IS_SUBAGENT") == "1":
            logger.info("[zeta] 子 agent 环境, 不加载")
            self._disabled_reason = "子 agent 环境（FP_IS_SUBAGENT=1）不提供邻居面。"
            self.disable()
            return
        lifecycle.register(LifecycleHook.ON_INIT, self._on_init, priority=60, name="zeta_init")
        lifecycle.register(LifecycleHook.ON_SHUTDOWN, self._on_shutdown, priority=60, name="zeta_shutdown")

    def on_unregister(self):
        self._unregister_tools()
        self._stop()

    # ── 启动 / 停止 ──────────────────────────────────

    def _start(self, sid: str = "") -> bool:
        ws = Path(os.getcwd())
        name = derive_instance_name(ws, sid)
        self._instances = InstanceRegistry(ws, name, single=os.environ.get("FP_ZETA_SINGLE") == "1")
        try:
            self._instances.acquire(sid=sid)
        except InstanceConflictError as exc:
            logger.warning(f"[zeta] {exc} —— 本实例不启用邻居通信")
            self._instances = None
            self._disabled_reason = f"实例登记冲突：{exc}。如需参与邻居协作，请换目录或改实例名。"
            return False

        # E(ii)：resume 跨目录 → 人格（会话）与位置（workspace）错配，记下待提示一次
        self._workspace_mismatch = check_session_workspace(sid, ws)

        self._backend = SharedDirBackend()
        self._card = build_card(name=name, cwd=ws, workspace=ws, business=infer_business(ws))
        self._backend.publish(self._card)
        self._service = PeerService(
            name=name,
            backend=self._backend,
            transport=DirTransport(),
            inject=jobs.inject_event,
            exchange=ContractExchange(self._backend.root / "contracts"),
        )
        self._task = asyncio.create_task(self._serve_loop())
        logger.info(f"[zeta] 邻居通信已启用: {name} (workspace={ws})")
        return True

    def _stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._backend is not None and self._card is not None:
            with contextlib.suppress(OSError):
                self._backend.withdraw(self._card.name)
        if self._instances is not None:
            self._instances.release()
            self._instances = None
        self._service = None

    async def _serve_loop(self) -> None:
        """心跳 + 收件轮询（后台）。铃声由 PeerService 经 jobs.inject_event 注入。"""
        while True:
            try:
                if self._service is not None:
                    self._service.poll()
                    self._service.housekeeping()  # 低频清理过期历史消息（内部节流）
                if self._backend is not None and self._card is not None:
                    refresh_heartbeat(self._card)
                    self._backend.publish(self._card)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 后台循环不可因单次异常退出
                logger.warning(f"[zeta] 服务循环异常: {exc}")
            await asyncio.sleep(self._poll_interval)

    # ── 钩子实现 ─────────────────────────────────────

    async def _on_init(self, ctx: HookContext, **kwargs: Any) -> HookContext:
        if os.environ.get("FP_ZETA_DISABLE") == "1":
            logger.info("[zeta] 已禁用 (FP_ZETA_DISABLE=1)")
            self._disabled_reason = "邻居面被 FP_ZETA_DISABLE=1 显式关闭。"
            # 显式关闭 = 用户明确不要邻居面：不注册工具、不注入说明（尊重显式意图）。
            return ctx
        self._start(self._resolve_sid(kwargs))

        # 无论启用与否都注册工具：让 LLM 知道"邻居能力存在"，降级时也能被告知原因，
        # 而不是静默消失（发现必须告知 LLM，否则双方都不会发现对方）。
        registry: ToolRegistry | None = kwargs.get("tool_registry")
        if registry is not None:
            self._registry = registry
            for nm, tool, fn in (
                ("peer_list", PEER_LIST_TOOL, self._exec_peer_list),
                ("peer_send", PEER_SEND_TOOL, self._exec_peer_send),
                ("peer_answer", PEER_ANSWER_TOOL, self._exec_peer_answer),
                ("peer_propose", PEER_PROPOSE_TOOL, self._exec_peer_propose),
                ("peer_ack", PEER_ACK_TOOL, self._exec_peer_ack),
            ):
                registry.register_tool(nm, cast(OpenAISchema, tool), fn)
                self._registered.append(nm)

        append = ctx.data.setdefault("system_prompt_append", [])
        if self._service is None:
            append.append(f"{ZETA_DESCRIPTION}\n（注意：本实例邻居面当前不可用——{self._degraded_msg()}）")
        else:
            append.append(ZETA_DESCRIPTION)
            if self._workspace_mismatch:
                append.append(
                    f"（注意：本会话原属于 workspace {self._workspace_mismatch}，"
                    f"当前 workspace 为 {os.getcwd()} —— 记忆读取当前目录的 .fp/。"
                    "这是 sid 与 workspace 解耦后的错配提示。）"
                )
        return ctx

    @staticmethod
    def _resolve_sid(kwargs: dict[str, Any]) -> str:
        """取当前会话 sid：优先 ON_INIT 传入的 state，回退进程级当前会话。"""
        state = kwargs.get("state")
        sid = str(getattr(state, "session_id", "") or "") if state is not None else ""
        return sid or (get_current_session_id() or "")

    async def _on_shutdown(self, ctx: HookContext, **kwargs: Any) -> HookContext:
        self._stop()
        return ctx

    # ── 工具执行 ─────────────────────────────────────

    def _unregister_tools(self) -> None:
        if self._registry is not None:
            for nm in self._registered:
                self._registry.unregister_tool(nm)
        self._registered.clear()
        self._registry = None

    def _degraded_msg(self) -> str:
        """降级原因（未启用邻居面时对 LLM 可见）。"""
        return self._disabled_reason or "Zeta 未启用。"

    async def _exec_peer_list(self, params: dict[str, Any]) -> str:
        if self._service is None:
            return self._degraded_msg()
        now = time.time()
        cards = self._service.neighbors()
        stamp = time.strftime("%H:%M:%S", time.localtime(now))
        header = f"邻居表（as of {stamp}）· 状态时变，寻址前请实时调用"
        if not cards:
            return f"{header}\n（当前没有在线邻居）"
        return header + "\n\n" + "\n\n".join(neighbor_brief(c, now) for c in cards)

    async def _exec_peer_send(self, params: dict[str, Any]) -> str:
        if self._service is None:
            return self._degraded_msg()
        to = str(params.get("to") or "").strip()
        body = str(params.get("body") or "").strip()
        topic = str(params.get("topic") or "chat").strip()
        if not to or not body:
            return "需要 to 与 body。可用 peer_list 查看邻居名。"
        msg_id, online = self._service.send(to, body, topic=topic)
        state = "在线" if online else "离线（已留话，待其上线）"
        return f"已投递给 peer:{to}（{state}）；msg_id={msg_id}"

    async def _exec_peer_answer(self, params: dict[str, Any]) -> str:
        if self._service is None:
            return self._degraded_msg()
        return self._service.answer(str(params.get("call_id") or "").strip())

    async def _exec_peer_propose(self, params: dict[str, Any]) -> str:
        if self._service is None:
            return self._degraded_msg()
        to = str(params.get("to") or "").strip()
        contract = params.get("contract")
        if not to or not isinstance(contract, dict):
            return "需要 to 与 contract（对象）。可用 peer_list 查看邻居名。"
        msg_id, online, issues = self._service.propose_contract(to, cast(dict[str, Any], contract))
        state = "在线" if online else "离线（已留话，待其上线）"
        head = f"契约提议已投递给 peer:{to}（{state}）；msg_id={msg_id}"
        if issues:
            return head + "\n本地机器校验未通过（对方会拒绝）：\n- " + "\n- ".join(issues)
        return head + "\n本地机器校验通过（hash 锁字节）。等对方 peer_ack 回执。"

    async def _exec_peer_ack(self, params: dict[str, Any]) -> str:
        if self._service is None:
            return self._degraded_msg()
        to = str(params.get("to") or "").strip()
        contract_id = str(params.get("contract_id") or "").strip()
        expected_hash = str(params.get("hash") or "").strip()
        accept = bool(params.get("accept", True))
        if not to or not contract_id or not expected_hash:
            return "需要 to / contract_id / hash。"
        msg_id, online = self._service.ack_contract(to, contract_id, expected_hash, accept=accept)
        verdict = "接受" if accept else "拒绝"
        state = "在线" if online else "离线（已留话）"
        return f"已回复 peer:{to}：{verdict}（hash={expected_hash[:12]}…，{state}）；msg_id={msg_id}"
