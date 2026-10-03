"""Zeta A2A HTTP 传输 —— 与外部 A2A agent 的真实网络通道（codec 层）。

只做两件事：**取 Agent Card**、**发一条消息拿回复**。wire 形状（路径/方法名/版本头/
part 形状）全部由 :mod:`a2a_codec` 决定，本模块只负责搬运与错误归一——保持
「翻译面可插拔」：换协议只换 codec，不动这里。

实测基线（2026-10，a2a-sdk 1.1.0 官方 helloworld）::

    GET  /.well-known/agent-card.json
    POST /   {"jsonrpc":"2.0","method":"SendMessage","params":{"message":{…}}}
             header: A2A-Version: 1.0

同步实现：Zeta 的 ``PeerService`` 本就是同步的（后台线程轮询），无需 async。
"""

from __future__ import annotations

import time
import uuid
from types import TracebackType
from typing import Any, cast

import httpx

from fp_core.plugins.zeta.a2a_codec import (
    CARD_PATH_V0,
    CARD_PATH_V1,
    PROTOCOL_V1,
    VERSION_HEADER,
    build_send_message_request,
    envelope_to_a2a_message,
    extract_reply_text,
    protocol_version_of,
)
from fp_core.plugins.zeta.domain import Envelope, MsgKind, Payload

__all__ = ["A2aHttpClient", "A2aTransportError"]


class A2aTransportError(RuntimeError):
    """与外部 A2A 对端通信失败（网络 / 协议 / 业务错误归一）。"""


class A2aHttpClient:
    """外部 A2A agent 的 HTTP 客户端。

    ``client`` 可注入（测试用 ``httpx.MockTransport`` / 自建 client）；默认自建并在
    ``close()`` 时释放。
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client if client is not None else httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._version = PROTOCOL_V1

    # ── 生命周期 ──
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> A2aHttpClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def protocol_version(self) -> str:
        """对端声明的协议版本（取卡后更新；默认 v1.0）。"""
        return self._version

    # ── 取卡 ──
    def fetch_card(self) -> dict[str, Any]:
        """取 Agent Card（先试 v1.0 路径，回退 v0.x）。"""
        last: Exception | None = None
        for path in (CARD_PATH_V1, CARD_PATH_V0):
            try:
                resp = self._client.get(self.base_url + path)
            except httpx.HTTPError as e:  # 网络层
                last = e
                continue
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    card = cast(dict[str, Any], data)
                    self._version = protocol_version_of(card) or PROTOCOL_V1
                    return card
        raise A2aTransportError(f"取 Agent Card 失败: {self.base_url} ({last!r})")

    # ── 发消息 ──
    def send_message(self, env: Envelope, *, rpc_id: str = "1") -> str:
        """投递一个 IR 信封，返回对端回复文本（抽取自 Task.artifacts / Message）。"""
        message = envelope_to_a2a_message(env, version=self._version)
        body = build_send_message_request(message, rpc_id=rpc_id, version=self._version)
        try:
            resp = self._client.post(
                self.base_url + "/",
                json=body,
                headers={VERSION_HEADER: self._version, "Content-Type": "application/json"},
            )
        except httpx.HTTPError as e:
            raise A2aTransportError(f"发送失败: {e}") from e
        try:
            data = resp.json()
        except ValueError as e:
            raise A2aTransportError(f"非 JSON 响应 (HTTP {resp.status_code})") from e
        if isinstance(data, dict):
            payload = cast(dict[str, Any], data)
            if payload.get("error"):
                err = cast(dict[str, Any], payload["error"])
                raise A2aTransportError(f"A2A 错误 {err.get('code')}: {err.get('message')}")
        return extract_reply_text(cast(dict[str, Any], data))

    def ask(
        self,
        text: str,
        *,
        from_: str = "zeta",
        to: str = "external",
        topic: str = "chat",
        correlation_id: str | None = None,
    ) -> str:
        """便捷入口：直接发一段文本，返回回复文本。"""
        env = Envelope(
            id=uuid.uuid4().hex,
            correlation_id=correlation_id or uuid.uuid4().hex,
            from_=from_,
            to=to,
            kind=MsgKind.SEMANTIC,
            payload=Payload(topic=topic, body=text),
            sent_at=time.time(),
        )
        return self.send_message(env)
