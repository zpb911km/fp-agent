"""A2A v1.0 HTTP 传输测试：wire 版本兼容 + Agent Card 解析 + 真实(可选)端到端。

**背景**：A2A 1.0 是破坏性升级（方法名 ``SendMessage``、card 走 ``supportedInterfaces``、
必需 ``A2A-Version`` 头、扁平 part）。本测试把「与官方实现实测一致的 wire 形状」钉死，
防止回归到 0.x 假设。

覆盖：
1. 入站解析**宽松**（v1.0 扁平 part 与 v0.x ``kind`` part 都能读出文本）；
2. 出站构造**按对端版本**（v1.0 vs v0.x 的 role / part / 方法名 / card 端点）；
3. ``A2aHttpClient`` 走 ``httpx.MockTransport``：取卡、发消息、错误归一；
4. 可选真实端到端：设 ``ZETA_A2A_E2E_URL`` 指向一个在跑的 A2A agent 时启用。
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any, cast

import httpx
import pytest

from fp_core.plugins.zeta.a2a_codec import (
    BARE_A2A,
    CARD_PATH_V1,
    METHOD_SEND_MESSAGE,
    PROTOCOL_V1,
    VERSION_HEADER,
    _text_of,  # noqa: PLC2701  # 私有但为 wire 契约的核心，直接钉
    build_send_message_request,
    card_from_a2a,
    envelope_to_a2a_message,
    extract_reply_text,
    protocol_version_of,
)
from fp_core.plugins.zeta.a2a_http import A2aHttpClient, A2aTransportError
from fp_core.plugins.zeta.domain import Envelope, MsgKind, NeighborKind, Payload


def _v1_card() -> dict[str, Any]:
    """与官方 helloworld 实测输出同构的 v1.0 卡片。"""
    return {
        "name": "Hello World Agent",
        "description": "Just a hello world agent",
        "supportedInterfaces": [
            {"url": "http://127.0.0.1:10099", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "version": "0.0.1",
        "capabilities": {"streaming": True, "extendedAgentCard": True},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [{"id": "echo_bot", "name": "Echo Bot", "tags": ["a2a"]}],
    }


def _env(text: str = "hi") -> Envelope:
    t = time.time()
    return Envelope(
        id=uuid.uuid4().hex,
        correlation_id="ctx-1",
        from_="zeta-a",
        to="external",
        kind=MsgKind.SEMANTIC,
        payload=Payload(topic="chat", body=text),
        sent_at=t,
    )


# ── 入站解析：宽松 ──────────────────────────────────────


def test_text_of_reads_v1_flat_part() -> None:
    msg = {"parts": [{"text": "hello v1", "mediaType": "text/plain"}]}
    assert _text_of(msg) == "hello v1"


def test_text_of_reads_v0_kind_part() -> None:
    msg = {"parts": [{"kind": "text", "text": "hello v0"}]}
    assert _text_of(msg) == "hello v0"


def test_card_from_a2a_v1_endpoint() -> None:
    card = card_from_a2a(_v1_card())
    assert card.kind is NeighborKind.EXTERNAL_A2A_AGENT
    assert card.endpoints and card.endpoints[0].address == "http://127.0.0.1:10099"
    assert card.capabilities.profiles == [BARE_A2A]  # 裸 A2A：底线能力，无 FP profile
    assert "echo_bot" in card.capabilities.skills


def test_card_from_a2a_v0_top_level_url() -> None:
    card = card_from_a2a({"name": "legacy", "url": "http://old:9999", "version": "0.3.0"})
    assert card.endpoints and card.endpoints[0].address == "http://old:9999"


def test_protocol_version_of() -> None:
    assert protocol_version_of(_v1_card()) == "1.0"
    assert protocol_version_of({"url": "http://x"}) == ""  # v0.x 无该字段


# ── 出站构造：按版本 ───────────────────────────────────


def test_envelope_to_message_v1_shape() -> None:
    msg = envelope_to_a2a_message(_env("body"), version=PROTOCOL_V1)
    assert msg["role"] == "ROLE_USER"
    assert msg["parts"] == [{"text": "body", "mediaType": "text/plain"}]
    assert "kind" not in msg  # v1.0 无 kind 字段
    assert msg["metadata"]["from"] == "zeta-a"


def test_envelope_to_message_v0_shape() -> None:
    msg = envelope_to_a2a_message(_env("body"), version="0.3.0")
    assert msg["role"] == "user"
    assert msg["parts"] == [{"kind": "text", "text": "body"}]


def test_build_send_message_request_method_names() -> None:
    v1 = build_send_message_request({"messageId": "m"}, version="1.0")
    assert v1["method"] == METHOD_SEND_MESSAGE == "SendMessage"
    assert v1["params"]["message"] == {"messageId": "m"}
    v0 = build_send_message_request({"messageId": "m"}, version="0.3.0")
    assert v0["method"] == "message/send"


# ── 响应提取 ───────────────────────────────────────────


def test_extract_reply_text_from_task_artifact() -> None:
    resp = {
        "result": {
            "task": {
                "id": "t1",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [{"parts": [{"text": "Hello, World!", "mediaType": "text/plain"}]}],
            }
        }
    }
    assert extract_reply_text(resp) == "Hello, World!"


def test_extract_reply_text_from_status_message() -> None:
    resp = {"result": {"task": {"status": {"message": {"parts": [{"text": "done"}]}}}}}
    assert extract_reply_text(resp) == "done"


# ── HTTP 客户端（MockTransport）────────────────────────


def _client(handler: Any) -> A2aHttpClient:
    return A2aHttpClient("http://ext:9999", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_http_fetch_card_sets_protocol_version() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        return httpx.Response(200, json=_v1_card())

    with _client(handler) as c:
        card = c.fetch_card()
        assert card["name"] == "Hello World Agent"
        assert c.protocol_version == "1.0"
    assert seen[0] == CARD_PATH_V1  # 先试 v1.0 路径


def test_http_fetch_card_falls_back_to_v0_path() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == CARD_PATH_V1:
            return httpx.Response(404)
        return httpx.Response(200, json={"name": "legacy", "url": "http://ext:9999"})

    with _client(handler) as c:
        card = c.fetch_card()
        assert card["name"] == "legacy"


def test_http_send_message_wire_shape_and_reply() -> None:
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        captured["method"] = req.headers.get(VERSION_HEADER)
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={"result": {"task": {"artifacts": [{"parts": [{"text": "pong"}]}]}}},
        )

    with _client(handler) as c:
        c._version = "1.0"
        reply = c.send_message(_env("ping"))
    assert reply == "pong"
    assert captured["method"] == "1.0"  # A2A-Version 头
    body = captured["body"]
    assert body["method"] == "SendMessage"
    assert body["params"]["message"]["parts"][0]["text"] == "ping"
    assert body["params"]["message"]["role"] == "ROLE_USER"


def test_http_send_message_raises_on_a2a_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"code": -32009, "message": "version mismatch"}})

    with _client(handler) as c, pytest.raises(A2aTransportError):
        c.send_message(_env())


def test_http_send_message_raises_on_non_json() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with _client(handler) as c, pytest.raises(A2aTransportError):
        c.send_message(_env())


# ── 可选真实端到端 ─────────────────────────────────────

_E2E_URL = os.environ.get("ZETA_A2A_E2E_URL", "")


@pytest.mark.skipif(not _E2E_URL, reason="设 ZETA_A2A_E2E_URL 指向在跑的 A2A agent 以启用")
def test_e2e_real_a2a_agent() -> None:
    """对真实 A2A agent 取卡 + 对话（验证 wire 形状与官方实现一致）。"""
    with A2aHttpClient(_E2E_URL) as c:
        card = c.fetch_card()
        assert cast(str, card.get("name"))
        assert c.protocol_version.startswith("1.")
        reply = c.ask("hello from zeta e2e")
        assert reply  # 官方 helloworld 会回 "Hello, World! I have received your request (...)"
