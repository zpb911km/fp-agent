"""fp-acp 线形状测试 — 断言出站 JSON-RPC 的结构与枚举符合 ACP v1 schema。

为什么需要这一层：现有用例只覆盖 elicitation 分支的逻辑，对「发出去的 update
长什么样」零断言，于是 ToolKind/plan 状态/messageId/toolCallId/cwd 这些漂移
长期不可见（历史上同类修复还被一次重复的 ``def send`` 静默抵消过）。
本文件只测「线上形状」与协议硬约束，不测业务语义。

规范来源：https://agentclientprotocol.com/protocol/v1
"""

from __future__ import annotations

import asyncio
import datetime
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
    """最小 SessionCtl 桩：只实现 handler 用到的方法。

    签名必须与真实 `SessionCtl` 同构（`new(cwd=, sid=)` / `generate_sid()`），
    否则 server 侧漏传 cwd、或把 sid 写错属性名时，桩会静默掩盖。
    """

    def __init__(
        self,
        *,
        load_result: Any = None,
        history: list[dict[str, Any]] | None = None,
        list_result: dict[str, dict[str, Any]] | None = None,
    ):
        self._load_result = load_result
        # None 表示「盘上无此会话」（真实实现 read_messages 的语义），
        # 不可退化成 []——否则 session/load 的存在性判断在桩下永远为真。
        self._history = history
        self._list_result = list_result or {}
        self.new_called = 0
        # 每次 new() 的实参，供断言「会话 cwd 是否随建档落盘」
        self.new_calls: list[dict[str, Any]] = []

    def generate_sid(self) -> str:
        """预分配 sid（session/new 只登记、不切 core 时需要先拿到 id）。"""
        return "sess_new"

    def new(self, cwd: str | None = None, sid: str | None = None):
        self.new_called += 1
        self.new_calls.append({"cwd": cwd, "sid": sid})

        class _Info:
            session_id = sid or "sess_new"

        return _Info()

    def load(self, sid: str):
        return self._load_result

    def list(self):
        return self._list_result

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
async def test_session_new_records_cwd_without_chdir(srv: ACPServer, monkeypatch, tmp_path):
    """规范 MUST：cwd 是会话根目录；但 new 只**登记**、不产生进程副作用。

    客户端常在 A 还在输出时开新 thread。若 new 里就 chdir/切 core，
    A 这一轮的输出会落进 B 的上下文。故 cwd 记进会话表，进入推迟到
    该会话首次 prompt（`_enter_session`，在 turn 锁内）。
    """
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions()))
    origin = os.getcwd()
    result = await srv._handle_session_new({"cwd": str(tmp_path), "mcpServers": []})
    assert result["sessionId"] == "sess_new"
    assert os.getcwd() == origin, "session/new 不得 chdir（会串掉正在跑的会话）"
    assert srv._sessions["sess_new"].cwd == str(tmp_path), "cwd 必须成为会话属性"


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
async def test_session_list_accepts_empty_params(srv: ACPServer, monkeypatch):
    """schema：zListSessionsRequest 四个字段全可选，客户端发 `{}` 必须成功。

    旧实现把 cwd 当必填抛 -32602 —— 而按 schema 写的客户端（含官方
    acp-client 扩展）只在知道 cwd 时才带参数，于是历史列表整个不可用。
    """
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions()))
    result = await srv._handle_session_list({})
    assert result["sessions"] == []
    assert "nextCursor" not in result


@pytest.mark.asyncio
async def test_session_list_filters_by_session_own_cwd(srv: ACPServer, monkeypatch, tmp_path):
    """cwd 过滤必须按**每一条会话自己的** cwd，不得拿当前会话目录冒充。

    旧实现把当前 cwd 回填给所有条目：别的项目的会话也会出现在本项目历史里，
    同时把真正的归属信息抹掉了。
    """
    proj = tmp_path / "proj_a"
    other = tmp_path / "proj_b"
    proj.mkdir()
    other.mkdir()
    metas = {
        "s_a": {"cwd": str(proj), "updated": "2026-11-19 10:00:00", "summary": "A"},
        "s_b": {"cwd": str(other), "updated": "2026-11-19 11:00:00", "summary": "B"},
        # 老会话：本版之前创建，meta 里没有 cwd
        "s_old": {"updated": "2026-11-19 09:00:00", "summary": "old"},
    }
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions(list_result=metas)))

    got = await srv._handle_session_list({"cwd": str(proj)})
    assert [s["sessionId"] for s in got["sessions"]] == ["s_a"]
    assert got["sessions"][0]["cwd"] == str(proj)

    # 软链/尾斜杠归一后应命中同一条
    got2 = await srv._handle_session_list({"cwd": str(proj) + "/"})
    assert [s["sessionId"] for s in got2["sessions"]] == ["s_a"]

    # 不带 cwd：全部返回（含老会话），老会话 cwd 不谎报、另标 cwdUnknown
    every = await srv._handle_session_list({})
    ids = [s["sessionId"] for s in every["sessions"]]
    assert ids == ["s_b", "s_a", "s_old"], "应按 updated 倒序"
    old = next(s for s in every["sessions"] if s["sessionId"] == "s_old")
    assert old["cwd"] == ""
    assert old["_meta"]["cwdUnknown"] is True


@pytest.mark.asyncio
async def test_session_list_paginates_with_cursor(srv: ACPServer, monkeypatch):
    """超过一页必须发 nextCursor，且翻页不重不漏（键值游标，非 offset）。"""
    metas = {
        f"s{i:03d}": {"cwd": "/tmp", "updated": f"2026-11-19 10:{i:02d}:00"} for i in range(srv_mod._LIST_PAGE_SIZE + 5)
    }
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions(list_result=metas)))

    page1 = await srv._handle_session_list({})
    assert len(page1["sessions"]) == srv_mod._LIST_PAGE_SIZE
    assert page1.get("nextCursor")

    page2 = await srv._handle_session_list({"cursor": page1["nextCursor"]})
    assert len(page2["sessions"]) == 5
    assert "nextCursor" not in page2, "最后一页不得再给游标"

    ids = [s["sessionId"] for s in page1["sessions"] + page2["sessions"]]
    assert len(ids) == len(set(ids)) == len(metas), "翻页不得重复或丢失"


@pytest.mark.asyncio
async def test_session_list_updated_at_is_iso8601(srv: ACPServer, monkeypatch):
    """schema：updatedAt 为 ISO 8601（meta 存的是 `%Y-%m-%d %H:%M:%S`）。"""
    metas = {"s_x": {"cwd": "/tmp", "updated": "2026-11-19 21:12:11"}}
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions(list_result=metas)))
    got = await srv._handle_session_list({})
    updated = got["sessions"][0]["updatedAt"]
    # 允许带本地时区偏移；关键是 "T" 分隔且可被 fromisoformat 解析
    assert "T" in updated
    datetime.datetime.fromisoformat(updated)


@pytest.mark.asyncio
async def test_prompt_unknown_session_id_is_error(srv: ACPServer):
    """此前完全忽略 params.sessionId（全局单会话），多会话必然串话。"""
    with pytest.raises(_ACPRPCError):
        await srv._handle_session_prompt({"sessionId": "other", "prompt": [{"type": "text", "text": "hi"}]})


@pytest.mark.asyncio
async def test_concurrent_prompt_is_request_error_not_cancelled(srv: ACPServer):
    """同会话并发是客户端 bug → -32600；回 stopReason=cancelled 会被误判为用户取消。"""
    srv._sessions["sess_test"] = srv_mod._SessionState(sid="sess_test", cwd=os.getcwd(), busy=True)
    await srv._dispatch({
        "jsonrpc": "2.0",
        "id": 7,
        "method": "session/prompt",
        "params": {"sessionId": "sess_test", "prompt": [{"type": "text", "text": "hi"}]},
    })
    (msg,) = _messages(srv)
    assert "error" in msg
    assert msg["error"]["code"] == -32600
    assert "result" not in msg


# ── 多会话：sessionId 是地址，不是「当前会话指针」 ──────────


@pytest.mark.asyncio
async def test_session_new_does_not_evict_previous(srv: ACPServer, monkeypatch, tmp_path):
    """客户端切 thread **不发协议消息**，它假设 agent 同时记着 N 条对话。

    旧实现只有单个 `_session_id`：第二个 session/new 一来就把第一个顶掉，
    回到旧 thread 直接 -32602，两条会话的输出还会串到对方面板上。
    """
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()

    class _S(_FakeSessions):
        def __init__(self):
            super().__init__()
            self._n = 0

        def generate_sid(self) -> str:
            self._n += 1
            return f"sess_{chr(96 + self._n)}"  # sess_a / sess_b

    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_S()))
    r1 = await srv._handle_session_new({"cwd": str(a)})
    r2 = await srv._handle_session_new({"cwd": str(b)})
    assert r1["sessionId"] != r2["sessionId"]
    assert set(srv._sessions) == {r1["sessionId"], r2["sessionId"]}, "先建的会话不得被顶掉"
    assert srv._sessions[r1["sessionId"]].cwd == str(a)
    assert srv._sessions[r2["sessionId"]].cwd == str(b)


@pytest.mark.asyncio
async def test_notifications_carry_owning_session_id(srv: ACPServer, monkeypatch):
    """turn 内所有通知必须带**该会话自己**的 sid（否则串到别的面板）。"""
    st = srv_mod._SessionState(sid="sess_owner", cwd=os.getcwd())
    srv._sessions[st.sid] = st
    srv._session_id = "sess_focused_other"
    srv._active = st
    srv._send_message_notification("hi", session_id=None)
    (msg,) = _messages(srv)
    assert msg["params"]["sessionId"] == "sess_owner"


@pytest.mark.asyncio
async def test_cancel_only_targets_its_session(srv: ACPServer, monkeypatch):
    """session/cancel 只中断目标会话；旧实现无脑 cancel 会把正在跑的别的会话打断。"""
    killed: list[int] = []
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions()))
    srv_mod.portal.run.cancel = lambda: killed.append(1)  # type: ignore[attr-defined]

    a = srv_mod._SessionState(sid="sess_a", cwd=os.getcwd())
    b = srv_mod._SessionState(sid="sess_b", cwd=os.getcwd())
    srv._sessions.update({a.sid: a, b.sid: b})
    srv._active = b  # b 正在跑

    srv._handle_cancel({"sessionId": "sess_a"})
    assert killed == [], "取消 A 不得打断正在跑的 B"
    srv._handle_cancel({"sessionId": "sess_b"})
    assert killed == [1], "取消正在跑的会话才置 core 中断标志"


@pytest.mark.asyncio
async def test_cross_session_prompt_queues_instead_of_rejecting(srv: ACPServer, monkeypatch, tmp_path):
    """跨会话并发是**合法**的：第二个 prompt 排队，不得回 -32600。

    为什么是排队不是拒绝：规范没有「必须并发执行」的 MUST，客户端也有权并发发
    prompt。拒绝会让客户端看到错误；排队它只看到「还在等」。core 只有一个执行槽
    /一套上下文，故靠 `_turn_lock` 串行 —— 但串行的前提是**每条会话各自记住
    自己的 cwd/上下文**，这正是会话表要保证的。
    """
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    # _enter_session 会问 core「当前是哪个会话」并按需建档，故 portal 必须打桩
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions()))
    st_a = srv_mod._SessionState(sid="sess_a", cwd=str(a))
    st_b = srv_mod._SessionState(sid="sess_b", cwd=str(b))
    srv._sessions.update({st_a.sid: st_a, st_b.sid: st_b})

    order: list[str] = []
    release = asyncio.Event()

    async def _fake_run(st: srv_mod._SessionState, prompt: str):
        order.append(f"enter:{st.sid}")
        if st.sid == "sess_a":
            await release.wait()  # 卡住 A，模拟「一轮还没跑完」
        order.append(f"exit:{st.sid}")
        return {"stopReason": "end_turn"}

    monkeypatch.setattr(srv, "_run_turn", _fake_run)

    t_a = asyncio.create_task(
        srv._handle_session_prompt({"sessionId": "sess_a", "prompt": [{"type": "text", "text": "A"}]})
    )
    await asyncio.sleep(0.02)  # 让 A 先拿到 turn 锁并进入执行
    t_b = asyncio.create_task(
        srv._handle_session_prompt({"sessionId": "sess_b", "prompt": [{"type": "text", "text": "B"}]})
    )
    await asyncio.sleep(0.02)

    assert order == ["enter:sess_a"], "B 必须排队：不得与 A 并行进入同一个执行槽"
    assert not t_b.done(), "B 应仍在排队等待（不是被拒绝返回）"

    release.set()
    r_a, r_b = await asyncio.gather(t_a, t_b)

    assert order == ["enter:sess_a", "exit:sess_a", "enter:sess_b", "exit:sess_b"]
    assert r_a["stopReason"] == r_b["stopReason"] == "end_turn", "两条会话都应正常跑完，而不是 -32600"


# ── 会话 cwd：core 绑定 + ACP 覆盖的叠加 ──────────────


def test_session_cwd_apply_is_noop_when_core_already_bound(srv: ACPServer, tmp_path):
    """core 装载会话时已把基准绑到 `meta.cwd` → ACP 不该再 set。

    改动前 ACP 是 cwd 的唯一施加者，所以它必须每轮 set；core 也绑之后两边都设
    就是重复写 contextvar，且 turn 结束的 reset 会回落成一个「中间态」。这里断言
    幂等：已绑好 → 返回 None（调用方不复位），值不变。
    """
    from fp_core.api.cwd import current_cwd, reset_cwd, set_cwd

    st = srv_mod._SessionState(sid="sess_x", cwd=str(tmp_path))
    token = set_cwd(str(tmp_path))
    try:
        assert srv._session_cwd_apply(st) is None, "基准已是该会话根 → 无需覆盖"
        assert current_cwd() == str(tmp_path)
    finally:
        reset_cwd(token)


def test_session_cwd_apply_overrides_client_declared_cwd(srv: ACPServer, tmp_path):
    """客户端在 `session/load` 声明的 cwd 与落盘 meta 不一致时，以客户端为准。"""
    from fp_core.api.cwd import current_cwd, reset_cwd, set_cwd

    other = tmp_path / "other"
    other.mkdir()
    st = srv_mod._SessionState(sid="sess_y", cwd=str(other))
    token = set_cwd(os.getcwd())
    applied = None
    try:
        applied = srv._session_cwd_apply(st)
        assert applied is not None, "与当前基准不同 → 必须覆盖并返回复位令牌"
        assert current_cwd() == str(other)
    finally:
        if applied is not None:
            reset_cwd(applied)
        reset_cwd(token)
    assert current_cwd() == os.getcwd(), "复位后回到本轮之前的值"


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
