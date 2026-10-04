"""fp-acp elicitation 单测 — 标准 elicitation/create（form）对接 ask_user。

契约：docs/acp/README.md「交互问答（ask_user → deferred / elicit）」。
覆盖：form schema 映射 / ACPIO 带内 elicit 五分派 / 无 elicit 回退 deferred /
      initialize 能力协商 / agent→client 请求-响应往返。
"""

from __future__ import annotations

import asyncio
import io
import json
import os

import pytest

from fp_acp.server import _CLIENT_CAPS_ENV, ACPIO, ACPServer, _build_form_schema


@pytest.fixture(autouse=True)
def _isolate_client_caps_env(monkeypatch):
    """隔离连接级能力环境变量：_handle_initialize 会写它，避免跨用例串味。"""
    monkeypatch.delenv(_CLIENT_CAPS_ENV, raising=False)


# ── form schema 映射 ──────────────────────────────────


def test_schema_options_to_enum_with_default():
    s = _build_form_schema(["A", "B"], "B")
    prop = s["properties"]["answer"]
    assert prop["enum"] == ["A", "B"]
    assert prop["default"] == "B"
    assert s["required"] == ["answer"]


def test_schema_free_text_uses_suggest_default():
    prop = _build_form_schema([], "默认")["properties"]["answer"]
    assert "enum" not in prop
    assert prop["default"] == "默认"


def test_schema_suggest_not_in_options_dropped():
    prop = _build_form_schema(["A", "B"], "C")["properties"]["answer"]
    assert "default" not in prop  # suggest 不属于 options 时不设 default


# ── ACPIO 带内 elicit 分派 ─────────────────────────────


def _io(elicit):
    chunks: list[str] = []
    return ACPIO(send_chunk=chunks.append, elicit=elicit), chunks


@pytest.mark.asyncio
async def test_ask_accept_returns_content():
    async def elicit(prompt, options, suggest):
        assert (prompt, options, suggest) == ("选哪个？", ["A", "B"], "A")
        return {"action": "accept", "content": {"answer": "B"}}

    io_, chunks = _io(elicit)
    reply = await io_.ask("选哪个？", options=["A", "B"], suggest="A")
    assert reply == "B"
    assert io_.ask_deferred is False
    assert chunks == []  # 带内成功，不走文本展示


@pytest.mark.asyncio
async def test_ask_decline_returns_empty_not_deferred():
    async def elicit(prompt, options, suggest):
        return {"action": "decline"}

    io_, _ = _io(elicit)
    reply = await io_.ask("选哪个？", options=["A"])
    assert reply == ""
    assert io_.ask_deferred is False  # 显式拒绝 ≠ deferred


@pytest.mark.asyncio
async def test_ask_cancel_returns_empty():
    async def elicit(prompt, options, suggest):
        return {"action": "cancel"}

    io_, _ = _io(elicit)
    assert await io_.ask("x") == ""
    assert io_.ask_deferred is False


@pytest.mark.asyncio
async def test_ask_elicit_failure_falls_back_to_deferred():
    async def elicit(prompt, options, suggest):
        raise RuntimeError("client error: -32602")

    io_, chunks = _io(elicit)
    reply = await io_.ask("选哪个？", options=["A"], suggest="A")
    assert reply == ""
    assert io_.ask_deferred is True
    assert chunks and "❓" in chunks[-1]  # 优雅回退为文本展示


@pytest.mark.asyncio
async def test_ask_without_elicit_is_deferred():
    io_, chunks = _io(None)
    assert io_.ask_deferred is True
    reply = await io_.ask("在吗？", options=["y", "n"], suggest="y")
    assert reply == ""
    assert chunks and "❓" in chunks[-1]


# ── initialize 能力协商 ────────────────────────────────


@pytest.mark.asyncio
async def test_initialize_reads_elicitation_form():
    srv = ACPServer()
    await srv._handle_initialize({
        "clientInfo": {"name": "zed", "version": "1.22.0"},
        "clientCapabilities": {"elicitation": {"form": {}, "url": {}}},
    })
    assert srv._client_elicitation == {"form": True, "url": True}


@pytest.mark.asyncio
async def test_initialize_without_elicitation():
    srv = ACPServer()
    await srv._handle_initialize({"clientCapabilities": {}})
    assert srv._client_elicitation == {"form": False, "url": False}


@pytest.mark.asyncio
async def test_initialize_elicitation_form_null_unsupported():
    srv = ACPServer()
    await srv._handle_initialize({"clientCapabilities": {"elicitation": {"form": None}}})
    assert srv._client_elicitation["form"] is False


# ── ACPIO 装配 ─────────────────────────────────────────


def test_make_acp_io_injects_elicit_when_form():
    srv = ACPServer()
    srv._client_elicitation = {"form": True, "url": False}
    io_ = srv._make_acp_io("s")
    assert io_._elicit is not None and io_.ask_deferred is False


def test_make_acp_io_without_form_is_deferred():
    srv = ACPServer()
    srv._client_elicitation = {"form": False, "url": False}
    io_ = srv._make_acp_io("s")
    assert io_._elicit is None and io_.ask_deferred is True


# ── agent→client 请求-响应往返 ─────────────────────────


@pytest.mark.asyncio
async def test_elicit_form_roundtrip():
    srv = ACPServer()
    sink = io.StringIO()
    srv._stdout = sink
    srv._session_id = "sess_1"

    task = asyncio.create_task(srv._elicit_form("sess_1", "选哪个？", ["A", "B"], "A"))
    await asyncio.sleep(0)  # 让 _elicit_form 写出请求并挂在 Future 上

    req = json.loads(sink.getvalue().strip().splitlines()[-1])
    assert req["method"] == "elicitation/create"
    assert req["params"]["mode"] == "form"
    assert req["params"]["sessionId"] == "sess_1"
    assert req["params"]["message"] == "选哪个？"
    assert req["params"]["requestedSchema"]["properties"]["answer"]["enum"] == ["A", "B"]

    # 模拟客户端带内响应 → 唤醒 Future
    srv._resolve_pending({
        "jsonrpc": "2.0",
        "id": req["id"],
        "result": {"action": "accept", "content": {"answer": "B"}},
    })
    result = await task
    assert result["action"] == "accept"
    assert result["content"]["answer"] == "B"
    assert srv._pending_requests == {}  # 未决已清理


@pytest.mark.asyncio
async def test_elicit_form_error_response_raises():
    srv = ACPServer()
    sink = io.StringIO()
    srv._stdout = sink

    task = asyncio.create_task(srv._elicit_form("s", "q", [], ""))
    await asyncio.sleep(0)
    req = json.loads(sink.getvalue().strip().splitlines()[-1])
    srv._resolve_pending({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32602, "message": "unsupported"}})
    with pytest.raises(RuntimeError):
        await task


@pytest.mark.asyncio
async def test_response_without_pending_is_noop():
    srv = ACPServer()
    srv._resolve_pending({"jsonrpc": "2.0", "id": 999, "result": {}})  # 不抛


# ── 连接级能力的跨 exec 持久化（reload） ────────────────
# reload = execve 重启进程、stdio 连接不变、客户端不重发 initialize。
# 能力写入环境变量随 exec 继承，新实例构造时恢复。


@pytest.mark.asyncio
async def test_initialize_persists_caps_to_env():
    caps = {"elicitation": {"form": {}, "url": {}}, "fs": {"readTextFile": True}}
    await ACPServer()._handle_initialize({"clientCapabilities": caps})
    assert json.loads(os.environ[_CLIENT_CAPS_ENV]) == caps


@pytest.mark.asyncio
async def test_reload_roundtrip_restores_elicitation(monkeypatch):
    """模拟 reload：旧实例 initialize 写入环境变量 → 新实例构造即恢复能力。"""
    old = ACPServer()
    await old._handle_initialize({"clientCapabilities": {"elicitation": {"form": {}, "url": {}}}})
    restored = ACPServer()  # exec 后的新进程：收不到 initialize，只能靠环境变量
    assert restored._client_elicitation == {"form": True, "url": True}
    assert restored._make_acp_io("s")._elicit is not None  # 恢复真的生效（走带内 elicit）


def test_no_env_keeps_default(monkeypatch):
    monkeypatch.delenv(_CLIENT_CAPS_ENV, raising=False)
    assert ACPServer()._client_elicitation == {"form": False, "url": False}


def test_corrupt_env_keeps_default(monkeypatch):
    monkeypatch.setenv(_CLIENT_CAPS_ENV, "{not json")
    assert ACPServer()._client_elicitation == {"form": False, "url": False}


def test_non_dict_env_keeps_default(monkeypatch):
    monkeypatch.setenv(_CLIENT_CAPS_ENV, json.dumps([1, 2]))
    assert ACPServer()._client_elicitation == {"form": False, "url": False}
