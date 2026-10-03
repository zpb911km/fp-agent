"""Zeta 对等服务 —— 来电管理、铃声注入、发送与接听。

这是「邻居」的行为核心：

- 发现结果 → 告知 LLM（``neighbor_brief``，见 plugin 装配）；
- 收到语义消息 → **不打断、只提示**（铃声，环顶注入）；
- LLM 主动调 ``peer_answer`` 接听 → 正文以 tool result 进对话。

与「人类优先」的分层一致：本地用户能**打断**你（run.cancel），
邻居来电只能**让你知道**（铃声注入）——这是对等的精确表达。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol, cast

from fp_core.plugins.zeta import codec
from fp_core.plugins.zeta.contracts import ContractExchange, contract_digest, validate_contract
from fp_core.plugins.zeta.discovery import DiscoveryBackend
from fp_core.plugins.zeta.domain import (
    ContentTrust,
    Envelope,
    IdempotencyWindow,
    InjectAs,
    InjectionHint,
    MsgKind,
    NeighborCard,
    Payload,
    ProfileNegotiation,
    ProfileState,
    Ttl,
    negotiate_profile,
    ring_line,
)
from fp_core.plugins.zeta.transport import DirTransport


class InjectFn(Protocol):
    """注入函数：把一条消息入队。``wake=True`` 表示空闲时应唤醒一轮消费它。"""

    def __call__(self, kind: str, content: str, *, wake: bool = False) -> None: ...


# 契约协商消息的 topic（信封里的载荷分类）
CONTRACT_TOPIC = "contract"
CONTRACT_PROFILE = "fp-contract"

# 语义消息默认存活期（秒）：过期即丢弃——收到但不处理（§5 离线/重复）。
# 与「留话」语义配套：留话有保鲜期，太久了就不再打扰收件人。
DEFAULT_TTL_SECONDS = 7 * 86400.0  # 7 天

# 本实例声明的 profile 档位：fp-contract-profile = 完整双锁；bare-a2a = 容忍异构对端的底线。
LOCAL_PROFILES = ("fp-contract-profile", "bare-a2a")


def encode_contract_msg(op: str, **fields: Any) -> str:
    """构造契约协商载荷（JSON，排序键 → 可复算）。"""
    return json.dumps({"profile": CONTRACT_PROFILE, "op": op, **fields}, ensure_ascii=False, sort_keys=True)


def parse_contract_msg(body: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(body)
    except (ValueError, TypeError):
        return None
    if isinstance(obj, dict) and cast(dict[str, Any], obj).get("profile") == CONTRACT_PROFILE:
        return cast(dict[str, Any], obj)
    return None


def contract_report(body: str, *, degraded: bool = False) -> str:
    """对契约提议做**本地确定性校验**，把机器结论附在正文后（双锁，LLM 只做决策）。

    这是「不采信对方声明」的落点：接听方在把内容交给 LLM 之前，先跑一遍校验器。

    ``degraded=True``（对端非 FP / 未知来源）时结论标 ``UNVERIFIED``：本地校验**照做**
    （降级只降带宽，不降信任检查），但如实声明「对方没有同等的机器校验保证」，
    提示 LLM 不可把结论当作双方对等的双锁通过。
    """
    msg = parse_contract_msg(body)
    if msg is None or msg.get("op") != "propose":
        return ""
    contract = msg.get("contract")
    if not isinstance(contract, dict):
        return "\n[机器校验] 提议格式非法：contract 缺失"
    contract_obj = cast(dict[str, Any], contract)
    issues = validate_contract(contract_obj)
    digest = contract_digest(contract_obj)
    declared = msg.get("hash")
    mismatch = "" if declared == digest else f"（hash 不符：声明={str(declared)[:12]}… 实得={digest[:12]}…）"
    label = "机器校验·UNVERIFIED" if degraded else "机器校验"
    if issues:
        return f"\n[{label}] 未通过：{'; '.join(issues)}"
    tail = "（对端非本协议实例，未做对等双锁）" if degraded else ""
    return f"\n[{label}] 结构与安全区通过；hash={digest[:12]}…{mismatch}{tail}"


@dataclass
class PendingCall:
    """一通未接来电（只存来电显示级信息 + 正文，正文在接听前不进上下文）。"""

    id: str
    from_: str
    topic: str
    body: str
    sent_at: float
    trust: ContentTrust = ContentTrust.TRUSTED_PEER
    degraded: bool = False  # 对端非 FP / 未知来源 → 结果标 UNVERIFIED

    def ring(self, now: float | None = None) -> str:
        t = time.time() if now is None else now
        return ring_line(self.from_, self.topic, t - self.sent_at)


class PeerService:
    """对等通信服务：发送 / 轮询 / 来电 / 接听。"""

    def __init__(
        self,
        *,
        name: str,
        backend: DiscoveryBackend,
        transport: DirTransport,
        inject: InjectFn,
        exchange: ContractExchange | None = None,
        local_profiles: list[str] | None = None,
    ) -> None:
        self.name = name
        self._backend = backend
        self._transport = transport
        self._inject = inject
        self._exchange = exchange
        self._local_profiles = list(local_profiles or LOCAL_PROFILES)
        self._calls: dict[str, PendingCall] = {}
        self._idem = IdempotencyWindow()  # 重投/重放去重（at-least-once → 恰好处理一次）
        self._last_housekeeping = 0.0

    # ── 发现 ─────────────────────────────────────────────

    def neighbors(self) -> list[NeighborCard]:
        return [c for c in self._backend.scan() if c.name != self.name]

    def find(self, name: str) -> NeighborCard | None:
        for c in self.neighbors():
            if c.name == name:
                return c
        return None

    def negotiate(self, name: str) -> ProfileNegotiation:
        """按对端名片协商 profile；对端未知 = 空能力（→ 降级/失败，保守）。"""
        card = self.find(name)
        remote = list(card.capabilities.profiles) if card is not None else []
        return negotiate_profile(self._local_profiles, remote)

    def is_degraded(self, name: str) -> bool:
        """与某邻居交互是否降级：非 FP 实例 / 未知来源 / 无共同档。

        降级 = 结果不可视作「对等双锁」，只保证本地确定性校验（schema 兜底）。
        """
        if self.find(name) is None:
            return True  # 陌生来源：保守标 UNVERIFIED
        neg = self.negotiate(name)
        return neg.degraded or neg.state is ProfileState.FAILED

    # ── 发送 ─────────────────────────────────────────────

    def send(
        self,
        to: str,
        body: str,
        *,
        topic: str = "chat",
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[str, bool]:
        """投递一条语义消息。返回 ``(msg_id, online)``——离线也投（留话）。

        TODO(n14) send 路由接线：当前恒走 DirTransport（文件收件箱），未按端点分派。
        应按邻居卡 ``endpoint.kind`` 路由——``a2a`` → :class:`a2a_http.A2aHttpClient`
        （外部异构 agent，已能独立工作），``local`` → DirTransport。此处只差分派逻辑。
        """
        online = self.find(to) is not None
        env = Envelope(
            id=uuid.uuid4().hex,
            correlation_id=correlation_id or uuid.uuid4().hex,
            from_=self.name,
            to=to,
            kind=MsgKind.SEMANTIC,
            payload=Payload(
                topic=topic,
                body=body,
                injection=InjectionHint(
                    inject_as=InjectAs.USER_MESSAGE,
                    trust_label=ContentTrust.TRUSTED_PEER,
                ),
            ),
            idempotency_key=idempotency_key,
            ttl=Ttl(deadline=time.time() + DEFAULT_TTL_SECONDS),
            sent_at=time.time(),
        )
        msg_id = self._transport.deliver(to, codec.encode(env))
        return msg_id, online

    # ── 契约协商（协作面：propose → 机器校验 → ack）───────

    def propose_contract(self, to: str, contract: dict[str, Any]) -> tuple[str, bool, list[str]]:
        """投递契约提议。返回 ``(msg_id, online, local_issues)``。

        提议方先自检（不自洽不出门）；本地校验通过且已装配交换区时，落盘（提议方写）。
        """
        issues = validate_contract(contract)
        digest = contract_digest(contract)
        if not issues and self._exchange is not None:
            self._exchange.publish(contract)
        body = encode_contract_msg("propose", contract=contract, hash=digest)
        msg_id, online = self.send(to, body, topic=CONTRACT_TOPIC)
        return msg_id, online, issues

    def ack_contract(
        self,
        to: str,
        contract_id: str,
        expected_hash: str,
        *,
        accept: bool,
        issues: list[str] | None = None,
    ) -> tuple[str, bool]:
        """回执：``accept=True`` 表示本地机器校验通过且确认方同意（hash 锁字节）。"""
        body = encode_contract_msg(
            "ack",
            contract_id=contract_id,
            hash=expected_hash,
            accept=accept,
            issues=issues or [],
        )
        return self.send(to, body, topic=CONTRACT_TOPIC)

    # ── 轮询（别人找我）──────────────────────────────────

    def poll(self) -> int:
        """取走 inbox 全部消息：语义消息 → 存来电 + 铃响；机械消息 → 丢弃（不进 LLM）。

        两道前置闸门（§5 离线/重复）：
        - **过期丢弃**：``ttl`` 到期的留话不再打扰收件人（收到但不处理）；
        - **幂等去重**：同一 ``(from_, idempotency_key 或 信封 id)`` 只处理一次——
          at-least-once 传输（重投/重放）不产生重复响铃。
        """
        n = 0
        for text in self._transport.drain(self.name):
            try:
                env = codec.decode(text)
            except (ValueError, KeyError, TypeError):
                continue
            if env.ttl.expired():
                continue  # 过期留话：收到但不处理
            key = f"{env.from_}:{env.idempotency_key or env.id}"
            if self._idem.check(key) is not None:
                continue  # 已见（cached / inflight）：重投只处理一次
            self._idem.remember(key)
            if not env.should_inject():
                continue  # 红线 #2：机械消息永不注入
            call = PendingCall(
                id=env.id,
                from_=env.from_,
                topic=env.payload.topic or "chat",
                body=env.payload.body or "",
                sent_at=env.sent_at,
                degraded=self.is_degraded(env.from_),
            )
            self._calls[call.id] = call
            self._inject("peer_ring", call.ring(), wake=True)  # 铃声：只提示不打断；空闲则唤醒一轮
            n += 1
        return n

    def housekeeping(self, *, now: float | None = None, min_interval: float = 300.0) -> int:
        """低频维护：清理 transport 里过期的历史消息文件。

        幂等窗口自带 purge（``check`` 时触发）；此处只管文件存储卫生。
        ``min_interval`` 节流，避免每个轮询周期都扫盘。
        """
        t = time.time() if now is None else now
        if t - self._last_housekeeping < min_interval:
            return 0
        self._last_housekeeping = t
        return self._transport.purge_processed(self.name)

    # ── 接听 ─────────────────────────────────────────────

    def pending(self) -> list[PendingCall]:
        return sorted(self._calls.values(), key=lambda c: c.sent_at)

    def answer(self, call_id: str = "") -> str:
        """接听一通来电，返回正文（将以 tool result 进对话）。空 id = 接最早一通。"""
        if not self._calls:
            return "没有待接来电。"
        if call_id:
            call = self._calls.pop(call_id, None)
            if call is None:
                return f"未找到来电 {call_id!r}（见铃声提示）。"
        else:
            call = self.pending()[0]
            self._calls.pop(call.id, None)
        rest = len(self._calls)
        tail = f"\n（还有 {rest} 通未接来电）" if rest else ""
        report = contract_report(call.body, degraded=call.degraded)
        origin = "外部/降级对端·UNVERIFIED" if call.degraded else "对等实例"
        return f"来自 peer:{call.from_}（主题={call.topic}，来源={origin}）的正文：\n{call.body}{report}{tail}"
