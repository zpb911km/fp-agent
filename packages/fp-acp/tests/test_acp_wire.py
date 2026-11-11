"""fp-acp 线形状测试 — 断言出站 JSON-RPC 的结构与枚举符合 ACP v1 schema。

为什么需要这一层：现有用例只覆盖 elicitation 分支的逻辑，对「发出去的 update
长什么样」零断言，于是 ToolKind/plan 状态/messageId/toolCallId/cwd 这些漂移
长期不可见（历史上同类修复还被一次重复的 ``def send`` 静默抵消过）。
本文件只测「线上形状」与协议硬约束，不测业务语义。

规范来源：https://agentclientprotocol.com/protocol/v1
"""

from __future__ import annotations

import io
import json
import os
from typing import Any

import pytest

from fp_acp import server as srv_mod
from fp_acp.server import ACPServer, _ACPRPCError

# ── ACP v1 权威枚举（来自官方 schema，改动须同步规范） ──
VALID_TOOL_KINDS = {
    "read",
    "edit",
    "delete",
    "move",
    "search",
    "execute",
    "think",
    "fetch",
    "switch_mode",
    "other",
}
VALID_TOOL_CALL_CONTENT_TYPES = {"content", "diff", "terminal"}
VALID_PLAN_STATUSES = {"pending", "in_progress", "completed"}


# ── 测试脚手架 ────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_client_caps_env(monkeypatch):
    """隔离能力环境变量，避免 _handle_initialize 的写入跨用例串味。"""
    monkeypatch.delenv(srv_mod._CLIENT_CAPS_ENV, raising=False)


@pytest.fixture
def srv() -> ACPServer:
    """构造已绑会话的 server，出站 JSON 落到可解析的 StringIO。"""
    s = ACPServer()
    s._stdout = io.StringIO()
    s._session_id = "sess_test"
    return s


def _messages(srv: ACPServer) -> list[dict[str, Any]]:
    """解析并清空出站 JSON-RPC 消息。"""
    raw = srv._stdout.getvalue()  # type: ignore[attr-defined]
    srv._stdout = io.StringIO()  # type: ignore[attr-defined]
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _updates(srv: ACPServer) -> list[dict[str, Any]]:
    """取出站 session/update 通知的 update 负载。"""
    return [m["params"]["update"] for m in _messages(srv) if m.get("method") == "session/update"]


class _FakeSessions:
    """最小 SessionCtl 桩：只实现 handler 用到的三个方法。"""

    def __init__(self, *, load_result: Any = None, history: list[dict[str, Any]] | None = None):
        self._load_result = load_result
        self._history = history or []
        self.new_called = 0

    def new(self):
        self.new_called += 1

        class _Info:
            session_id = "sess_new"

        return _Info()

    def load(self, sid: str):
        return self._load_result

    def list(self):
        return {}

    def read_messages(self, sid: str):
        """真实路径是 ctl.sessions.read_messages（SessionCtl 下），
        桩必须与其同构，否则 server 侧写错属性名会被桩静默掩盖。"""
        return self._history


class _FakePortal:
    def __init__(self, sessions: _FakeSessions):
        self.ctl = type("Ctl", (), {})()
        self.ctl.sessions = sessions  # type: ignore[attr-defined]
        self.run = type("Run", (), {"status": type("S", (), {"session_id": "sess_test"})()})()


# ── ToolKind：schema 枚举硬约束 ──────────────────────


@pytest.mark.parametrize(
    "tool_name",
    ["bash", "read_file", "write_file", "edit_file", "python", "web_search", "subagent", "web_fetch", "unknown_tool"],
)
def test_tool_kind_in_schema_enum(srv: ACPServer, tool_name: str):
    """任何工具名映射出的 kind 都必须是 schema 枚举内的值（旧实现会发 bash/analyze）。"""
    assert srv._get_tool_kind(tool_name) in VALID_TOOL_KINDS


@pytest.mark.parametrize(("tool_name", "expected"), [("bash", "execute"), ("unknown_tool", "other")])
def test_tool_kind_mapping(srv: ACPServer, tool_name: str, expected: str):
    assert srv._get_tool_kind(tool_name) == expected


# ── tool_call / tool_call_update 形状 ────────────────


@pytest.mark.asyncio
async def test_tool_call_shape(srv: ACPServer):
    await srv._on_tool_call(None, tool_name="bash", tool_args='{"command": "ls -l"}', tool_call_id="call_1")
    (update,) = _updates(srv)

    assert update["sessionUpdate"] == "tool_call"
    assert update["toolCallId"] == "call_1"  # 用真实 call id 配对
    assert update["kind"] in VALID_TOOL_KINDS
    assert update["status"] == "in_progress"
    # rawInput 的 schema 类型是 object（不是格式化字符串）
    assert isinstance(update["rawInput"], dict)
    # content 元素必须是 ToolCallContent union 变体
    assert update["content"], "content 不能为空"
    for item in update["content"]:
        assert item["type"] in VALID_TOOL_CALL_CONTENT_TYPES
        if item["type"] == "content":
            assert isinstance(item["content"], dict)
            assert "type" in item["content"]


@pytest.mark.asyncio
async def test_tool_result_pairs_by_real_call_id(srv: ACPServer, tmp_path):
    """并行的同名工具不能互相错配（旧实现以工具名为键，后者覆盖前者）。"""
    for cid in ("call_a", "call_b"):
        await srv._on_tool_call(None, tool_name="bash", tool_args='{"command": "true"}', tool_call_id=cid)
    _messages(srv)

    await srv._on_tool_result(None, tool_name="bash", result="done", tool_call_id="call_a")
    (upd_a,) = _updates(srv)
    await srv._on_tool_result(None, tool_name="bash", result="done", tool_call_id="call_b")
    (upd_b,) = _updates(srv)

    assert upd_a["toolCallId"] == "call_a"
    assert upd_b["toolCallId"] == "call_b"


@pytest.mark.asyncio
async def test_tool_result_content_and_no_raw_output(srv: ACPServer):
    await srv._on_tool_result(None, tool_name="bash", result="输出内容", tool_call_id="call_1")
    (update,) = _updates(srv)

    assert update["status"] == "completed"
    # 工具结果是字符串，rawOutput 的 schema 类型是 object → 不得填字符串
    assert "rawOutput" not in update
    for item in update.get("content", []):
        assert item["type"] in VALID_TOOL_CALL_CONTENT_TYPES


@pytest.mark.asyncio
async def test_edit_diff_path_is_absolute(srv: ACPServer):
    await srv._on_tool_call(
        None,
        tool_name="edit_file",
        tool_args='{"file_path": "rel.py", "old_string": "a", "new_string": "b"}',
        tool_call_id="call_e",
    )
    _messages(srv)
    await srv._on_tool_result(None, tool_name="edit_file", result="ok", tool_call_id="call_e")
    (update,) = _updates(srv)

    diffs = [i for i in update.get("content", []) if i["type"] == "diff"]
    assert diffs, "edit_file 应带 diff 块"
    assert os.path.isabs(diffs[0]["path"])


# ── messageId 语义 ────────────────────────────────────


@pytest.mark.asyncio
async def test_message_id_stable_within_turn(srv: ACPServer):
    """同一条回复的所有 chunk 共享一个 messageId（id 变化 = 新消息开始）。"""
    acp_io = srv._make_acp_io("sess_test")
    for piece in ("第一段", "第二段", "第三段"):
        acp_io._accumulate(piece)
        acp_io._partial_flush()

    chunks = [u for u in _updates(srv) if u.get("sessionUpdate") == "agent_message_chunk"]
    assert len(chunks) >= 2, "应产生多个流式 chunk 才能验证 messageId 稳定性"
    ids = {u["messageId"] for u in chunks}
    assert len(ids) == 1, f"同一回合出现多个 messageId: {ids}"


# ── plan 状态枚举 ─────────────────────────────────────


def test_plan_status_enum_enforced(srv: ACPServer):
    """非法状态（如历史上用过的 "running"）必须被纠正为枚举内值。"""
    srv._send_plan_notification("sess_test", status="running")
    (update,) = _updates(srv)
    assert update["sessionUpdate"] == "plan"
    for entry in update["entries"]:
        assert entry["status"] in VALID_PLAN_STATUSES


# ── initialize：版本协商与元数据 ──────────────────────


@pytest.mark.asyncio
async def test_initialize_negotiates_protocol_version(srv: ACPServer):
    """客户端请求本端不支持的版本（如 v2 Draft）时回退到 v1，而不是假装支持。"""
    result = await srv._handle_initialize({"protocolVersion": 2, "clientCapabilities": {}})
    assert result["protocolVersion"] == 1
    result = await srv._handle_initialize({"protocolVersion": 1, "clientCapabilities": {}})
    assert result["protocolVersion"] == 1


@pytest.mark.asyncio
async def test_initialize_agent_version_from_metadata(srv: ACPServer):
    """agentInfo.version 取包元数据，不得硬编码（旧实现写死 0.1.0）。"""
    result = await srv._handle_initialize({"protocolVersion": 1, "clientCapabilities": {}})
    assert result["agentInfo"]["version"] != "0.1.0"
    assert result["agentInfo"]["name"] == "fp"


# ── session/new 与 cwd ───────────────────────────────


@pytest.mark.asyncio
async def test_session_new_requires_cwd(srv: ACPServer):
    with pytest.raises(_ACPRPCError) as ei:
        await srv._handle_session_new({})
    assert ei.value.code == -32602


@pytest.mark.asyncio
async def test_session_new_applies_cwd(srv: ACPServer, monkeypatch, tmp_path):
    """规范 MUST：cwd 是会话根目录，相对路径解析以它为基准。"""
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions()))
    origin = os.getcwd()
    try:
        result = await srv._handle_session_new({"cwd": str(tmp_path), "mcpServers": []})
        assert result["sessionId"] == "sess_new"
        assert os.getcwd() == str(tmp_path)
        assert srv._session_cwd == str(tmp_path)
    finally:
        os.chdir(origin)


@pytest.mark.asyncio
async def test_session_new_rejects_missing_cwd_dir(srv: ACPServer, tmp_path):
    with pytest.raises(_ACPRPCError):
        await srv._handle_session_new({"cwd": str(tmp_path / "not-exists")})


# ── session/load：历史回放 ───────────────────────────


@pytest.mark.asyncio
async def test_session_load_replays_history(srv: ACPServer, monkeypatch, tmp_path):
    """规范 MUST：响应之前必须用 session/update 回放整个会话。"""
    history = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好，我是 FP"},
        {"role": "system", "content": "系统提示不该回放"},
    ]

    class _Info:
        session_id = "sess_old"

    fake = _FakeSessions(load_result=_Info(), history=history)
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(fake))
    origin = os.getcwd()
    try:
        result = await srv._handle_session_load({"sessionId": "sess_old", "cwd": str(tmp_path)})
    finally:
        os.chdir(origin)

    assert result["sessionId"] == "sess_old"
    kinds = [u["sessionUpdate"] for u in _updates(srv)]
    assert kinds == ["user_message_chunk", "agent_message_chunk"]
    assert "system" not in json.dumps(kinds)


@pytest.mark.asyncio
async def test_session_load_current_memory_session_ok(srv: ACPServer, monkeypatch, tmp_path):
    """刚 session/new 的会话尚未落盘（惰性建文件）——load 它必须成功且回放为空，
    否则 IDE 重连恢复一个空会话会报「不存在」。"""
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions(load_result=None, history=[])))
    origin = os.getcwd()
    try:
        result = await srv._handle_session_load({"sessionId": "sess_test", "cwd": str(tmp_path)})
    finally:
        os.chdir(origin)
    assert result["sessionId"] == "sess_test"
    assert _updates(srv) == []  # 历史为空 → 只有响应，无回放通知


@pytest.mark.asyncio
async def test_session_load_unknown_session_is_error(srv: ACPServer, monkeypatch, tmp_path):
    """会话不存在应报错（旧实现静默新建空会话，IDE 以为历史已恢复）。"""
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions(load_result=None)))
    with pytest.raises(_ACPRPCError) as ei:
        await srv._handle_session_load({"sessionId": "nope", "cwd": str(tmp_path)})
    assert ei.value.code == -32602


# ── session/list 与 prompt 的协议错误 ────────────────


@pytest.mark.asyncio
async def test_session_list_requires_cwd(srv: ACPServer):
    with pytest.raises(_ACPRPCError) as ei:
        await srv._handle_session_list({})
    assert ei.value.code == -32602


@pytest.mark.asyncio
async def test_prompt_unknown_session_id_is_error(srv: ACPServer):
    """此前完全忽略 params.sessionId（全局单会话），多会话必然串话。"""
    with pytest.raises(_ACPRPCError):
        await srv._handle_session_prompt({"sessionId": "other", "prompt": [{"type": "text", "text": "hi"}]})


@pytest.mark.asyncio
async def test_concurrent_prompt_is_request_error_not_cancelled(srv: ACPServer):
    """并发冲突是请求错误；回 stopReason=cancelled 会被客户端误判为用户取消。"""
    srv._processing_prompt = True
    await srv._dispatch({
        "jsonrpc": "2.0",
        "id": 7,
        "method": "session/prompt",
        "params": {"sessionId": "sess_test", "prompt": [{"type": "text", "text": "hi"}]},
    })
    srv._processing_prompt = False
    (msg,) = _messages(srv)
    assert "error" in msg
    assert msg["error"]["code"] == -32600
    assert "result" not in msg


# ── prompt content blocks ────────────────────────────


def test_resource_link_not_dropped(srv: ACPServer):
    """baseline MUST：resource_link 必须被支持（旧实现静默丢弃，@文件引用消失）。"""
    text = srv._prompt_blocks_to_text([
        {"type": "resource_link", "uri": "file:///tmp/a.py", "name": "a.py", "mimeType": "text/x-python"}
    ])
    assert "a.py" in text
    assert "file:///tmp/a.py" in text


def test_text_block_passthrough(srv: ACPServer):
    assert srv._prompt_blocks_to_text([{"type": "text", "text": "hi"}]) == "hi"


# ── 未声明的方法不得存在 ─────────────────────────────


def test_set_mode_not_advertised_nor_handled(srv: ACPServer):
    """未在 initialize 声明 modes/availableModes 时，不应提供伪 set_mode。"""
    assert srv._get_handler("session/set_mode") is None


@pytest.mark.asyncio
async def test_unimplemented_method_returns_method_not_found(srv: ACPServer):
    await srv._dispatch({"jsonrpc": "2.0", "id": 9, "method": "session/set_mode", "params": {}})
    (msg,) = _messages(srv)
    assert msg["error"]["code"] == -32601
