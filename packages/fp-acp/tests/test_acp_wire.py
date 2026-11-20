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
        titles: dict[str, dict[str, Any]] | None = None,
    ):
        self._load_result = load_result
        # 任意会话的 meta（真实实现是 SessionManager.meta_of，读盘、只读）。
        # 刻意**不**提供 `meta` 属性：那是 property（零参、只认当前会话），
        # server 侧若误用 `.meta(sid)` 会在这里抛错——桩不能替它把错误吞掉。
        self._titles = titles or {}
        # None 表示「盘上无此会话」（真实实现 read_messages 的语义），
        # 不可退化成 []——否则 session/load 的存在性判断在桩下永远为真。
        self._history = history
        self._list_result = list_result or {}
        self.new_called = 0
        # 每次 new() 的实参，供断言「会话 cwd 是否随建档落盘」
        self.new_calls: list[dict[str, Any]] = []
        # session/delete 的实参（真实实现 SessionCtl.delete → 删盘 + 删 meta）
        self.deleted: list[str] = []
        # 盘上实际存在、可被 delete 的会话 id；其余 delete 返回 False（语义同真实实现）
        self.deletable: set[str] = set()
        # update_meta 的调用（session/load|resume 的 cwd 回填路径用）
        self.update_meta_calls: list[dict[str, Any]] = []

    def generate_sid(self) -> str:
        """预分配 sid（session/new 只登记、不切 core 时需要先拿到 id）。"""
        return "sess_new"

    def meta_of(self, sid: str) -> dict[str, Any]:
        """任意会话的 meta（真实实现只读盘、不 switch）。"""
        return dict(self._titles.get(sid) or {})

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

    def delete(self, sid: str) -> bool:
        """真实实现 SessionCtl.delete：删磁盘历史 + meta，返回「原本是否存在」。"""
        self.deleted.append(sid)
        existed = sid in self.deletable
        self.deletable.discard(sid)
        self._list_result.pop(sid, None)
        self._titles.pop(sid, None)
        return existed

    def update_meta(self, sid: str, **fields: Any) -> None:
        self.update_meta_calls.append({"sid": sid, **fields})
        self._list_result.setdefault(sid, {}).update({k: v for k, v in fields.items() if v is not None})


class _FakePlugins:
    """`ctl.plugins` 的最小桩：只实现 `get(name)`。"""

    def __init__(self, mapping: dict[str, Any] | None = None):
        self._mapping = mapping or {}
        self.get_calls: list[str] = []

    def get(self, name: str):
        self.get_calls.append(name)
        return self._mapping.get(name)


class _FakePortal:
    def __init__(
        self,
        sessions: _FakeSessions,
        plugins: Any = None,
        commands: dict[str, Any] | None = None,
        on_command: Any = None,
    ):
        self.ctl = type("Ctl", (), {})()
        self.ctl.sessions = sessions  # type: ignore[attr-defined]
        self.ctl.plugins = plugins if plugins is not None else _FakePlugins()  # type: ignore[attr-defined]
        run = type("Run", (), {})()
        run.status = type("S", (), {"session_id": "sess_test"})()
        # 命令层：session/set_config_option 走 `run.command("/model <id>")`。
        # commands 决定「/model 是否安装」，on_command 决定命令做了什么副作用。
        run.commands = dict(commands or {})
        run.command_calls = []
        self.run = run

        async def _command(text: str) -> tuple[bool, str]:
            run.command_calls.append(text)
            if on_command is not None:
                return on_command(text)
            return True, ""

        run.command = _command


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


# ── 客户端转发的 MCP server：会话私有（方案 B）───────────


class _FakeMcpPlugin:
    """mcp 插件的会话级接口桩，记录调用并可控拒绝。"""

    def __init__(self, rejected: list[str] | None = None):
        self.added: list[tuple[str, dict[str, Any]]] = []
        self.dropped: list[str] = []
        self._rejected = rejected or []

    def add_session_servers(self, sid: str, servers: dict[str, Any]) -> list[str]:
        self.added.append((sid, servers))
        return self._rejected

    def drop_session_servers(self, sid: str) -> None:
        self.dropped.append(sid)


@pytest.mark.asyncio
async def test_session_new_registers_mcp_servers_as_session_private(srv: ACPServer, monkeypatch, tmp_path):
    """IDE 传入的 mcpServers 必须**登记到该会话名下**，而不是全局。

    这是方案 B 的核心断言：A 会话的 server 不能出现在 B 会话的工具面里。
    """
    plug = _FakeMcpPlugin()
    fake = _FakeSessions()
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(fake, _FakePlugins({"mcp": plug})))
    servers = {"fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-fs"]}}
    await srv._handle_session_new({"cwd": str(tmp_path), "mcpServers": servers})
    assert plug.added == [("sess_new", servers)], "必须带 sid 登记，否则退化成全局可见"


@pytest.mark.asyncio
async def test_session_new_without_mcp_servers_does_not_touch_plugin(srv: ACPServer, monkeypatch, tmp_path):
    plug = _FakeMcpPlugin()
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions(), _FakePlugins({"mcp": plug})))
    await srv._handle_session_new({"cwd": str(tmp_path)})
    assert plug.added == []


@pytest.mark.asyncio
async def test_session_load_registers_mcp_servers_as_session_private(srv: ACPServer, monkeypatch, tmp_path):
    """`session/load` 也要登记：IDE 切 thread 时同样会带 mcpServers。

    只测 session/new 不足以守护 load 分支——两处调用互相独立，删掉 load 那句
    session/new 的用例照样绿，而真实使用里「恢复已有 thread」才是最常走的路径。
    """
    plug = _FakeMcpPlugin()
    fake = _FakeSessions(load_result=type("_I", (), {"session_id": "sess_old"})(), history=[])

    monkeypatch.setattr(srv_mod, "portal", _FakePortal(fake, _FakePlugins({"mcp": plug})))
    servers = {"db": {"command": "uvx", "args": ["mcp-db"]}}
    origin = os.getcwd()
    try:
        await srv._handle_session_load({"sessionId": "sess_old", "cwd": str(tmp_path), "mcpServers": servers})
    finally:
        os.chdir(origin)
    assert plug.added == [("sess_old", servers)], "load 路径必须按该会话 sid 登记"


@pytest.mark.asyncio
async def test_session_close_reachable_via_dispatch(srv: ACPServer, monkeypatch):
    """`session/close` 必须真的挂进 dispatch 表。

    直接调 `_handle_session_close` 只能证明逻辑对；漏掉 dispatch 表里的那一行，
    handler 就成了死代码，客户端一调就是 -32601（方法不存在），而所有单测仍绿。
    """
    plug = _FakeMcpPlugin()
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions(), _FakePlugins({"mcp": plug})))
    srv._sessions["sess_x"] = srv_mod._SessionState(sid="sess_x", cwd="/tmp")
    await srv._dispatch({"jsonrpc": "2.0", "id": 11, "method": "session/close", "params": {"sessionId": "sess_x"}})
    (msg,) = _messages(srv)
    assert "error" not in msg, f"session/close 未接线 → {msg.get('error')}"
    assert msg["result"] == {}
    assert plug.dropped == ["sess_x"]


@pytest.mark.asyncio
async def test_session_new_actually_triggers_eviction(srv: ACPServer, monkeypatch, tmp_path):
    """`session/new` 这条**调用路径**必须真的走到淘汰。

    `test_evict_sessions_keeps_current_and_busy` 直接调 `_evict_sessions()`，
    只证明淘汰逻辑对；把 session/new 里那句调用删掉它照样绿（变异实测存活）。
    长连接靠的就是「每建一条新会话顺手淘汰一次」，漏掉接线则 `_sessions`
    在真实使用里照旧只增不减 —— 逻辑正确但从未执行，等于没做。
    """
    plug = _FakeMcpPlugin()
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions(), _FakePlugins({"mcp": plug})))
    monkeypatch.setattr(srv_mod, "_MAX_SESSIONS", 2)
    # 先摊一条「旧」会话与一条当前会话（busy 与当前都不可淘汰，此处留出可淘汰位）
    srv._sessions.update({
        "old": srv_mod._SessionState(sid="old", cwd="/tmp"),
        "cur": srv_mod._SessionState(sid="cur", cwd="/tmp"),
    })
    srv._session_id = "cur"

    await srv._handle_session_new({"cwd": str(tmp_path)})

    assert "sess_new" in srv._sessions
    assert "old" not in srv._sessions, "session/new 未触发淘汰 → 长连接会话登记只增不减"
    assert plug.dropped == ["old"], "被淘汰会话的私有 MCP 也必须一起回收"


@pytest.mark.asyncio
async def test_session_new_tolerates_missing_mcp_plugin(srv: ACPServer, monkeypatch, tmp_path):
    """未加载 mcp 插件时（或 ctl.plugins 不可用），session/new 不得因此失败。"""
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions(), _FakePlugins({})))
    result = await srv._handle_session_new({"cwd": str(tmp_path), "mcpServers": {"fs": {"command": "x"}}})
    assert result["sessionId"] == "sess_new"


@pytest.mark.asyncio
async def test_session_close_drops_session_mcp(srv: ACPServer, monkeypatch):
    plug = _FakeMcpPlugin()
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions(), _FakePlugins({"mcp": plug})))
    srv._sessions["sess_x"] = srv_mod._SessionState(sid="sess_x", cwd="/tmp")
    await srv._handle_session_close({"sessionId": "sess_x"})
    assert plug.dropped == ["sess_x"], "关闭会话必须回收其私有 server 进程"
    assert "sess_x" not in srv._sessions


@pytest.mark.asyncio
async def test_session_close_is_idempotent(srv: ACPServer, monkeypatch):
    """重复 close 不报错（客户端重试/多路关闭都合法）。"""
    plug = _FakeMcpPlugin()
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions(), _FakePlugins({"mcp": plug})))
    await srv._handle_session_close({"sessionId": "nope"})
    await srv._handle_session_close({"sessionId": "nope"})
    assert plug.dropped == ["nope", "nope"]


@pytest.mark.asyncio
async def test_session_close_requires_session_id(srv: ACPServer, monkeypatch):
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions()))
    with pytest.raises(_ACPRPCError) as ei:
        await srv._handle_session_close({})
    assert ei.value.code == -32602


@pytest.mark.asyncio
async def test_session_close_advertised(srv: ACPServer, monkeypatch):
    """声明了 sessionCapabilities.close 才允许客户端调用。"""
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions()))
    monkeypatch.setattr(srv, "_send_available_commands", lambda: None)
    caps = await srv._handle_initialize({"protocolVersion": 1})
    assert "close" in caps["agentCapabilities"]["sessionCapabilities"]


@pytest.mark.asyncio
async def test_evict_sessions_keeps_current_and_busy(srv: ACPServer, monkeypatch):
    """超过上限时淘汰最久未用者，但不动当前聚焦会话与正在输出的会话。

    注意 **busy 必须排在插入序首位**：`_evict_sessions` 用 next() 取第一个
    可淘汰者，若把 busy 放在 "old" 之后，即使删掉 `not st.busy` 判断也仍然
    先挑到 "old"，这条测试就永远杀不掉那个变异（实测存活过）。
    """
    plug = _FakeMcpPlugin()
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions(), _FakePlugins({"mcp": plug})))
    monkeypatch.setattr(srv_mod, "_MAX_SESSIONS", 3)
    srv._sessions.clear()
    srv._session_id = "cur"
    srv._sessions.update({
        "busy": srv_mod._SessionState(sid="busy", cwd="/tmp", busy=True),
        "old": srv_mod._SessionState(sid="old", cwd="/tmp"),
        "cur": srv_mod._SessionState(sid="cur", cwd="/tmp"),
    })
    srv._evict_sessions()  # 3 条 == 上限，不淘汰
    assert set(srv._sessions) == {"old", "busy", "cur"}
    srv._sessions["new4"] = srv_mod._SessionState(sid="new4", cwd="/tmp")
    srv._evict_sessions()  # 4 > 3 → 跳过 busy/cur，淘汰 "old"
    assert set(srv._sessions) == {"busy", "cur", "new4"}
    assert plug.dropped == ["old"], "被淘汰会话的私有 MCP 必须回收"


# ── 上下文改写通知（session.compaction）─────────────────────

# 协议枚举（v1 agent.rs CompactionStatus）
VALID_COMPACTION_STATUSES = {"in_progress", "completed", "failed", "cancelled"}


def _ctx(n_user: int, *, summary: bool = False) -> list[dict[str, Any]]:
    """造一份会话上下文（含 1 条 system 头），n_user 条 user 消息。"""
    msgs: list[dict[str, Any]] = [{"role": "system", "content": "SOUL"}]
    if summary:
        msgs.append({"role": "system", "content": "以下是压缩后的对话历史摘要…"})
    msgs += [{"role": "user", "content": f"msg-{i}"} for i in range(n_user)]
    return msgs


@pytest.mark.asyncio
async def test_compaction_emits_in_progress_then_completed(srv: ACPServer):
    """真压缩：in_progress → completed，且**同一个 compactionId**。

    客户端按 id 就地打补丁；换 id 会在时间线上留下两条实体（一条永远卡在
    in_progress），这是 ACP 里最典型的错误用法。
    """
    srv._client_compaction = True
    before = _ctx(20)
    after = _ctx(4, summary=True)
    await srv._on_compact_start(None, name="/compact", messages=before)
    await srv._on_compact_end(None, name="/compact", messages=after, handled=True, output="**📦**")
    ups = _updates(srv)
    assert [u["status"] for u in ups] == ["in_progress", "completed"]
    assert all(u["sessionUpdate"] == "compaction_update" for u in ups)
    assert ups[0]["compactionId"] == ups[1]["compactionId"], "开态与终态必须同 id"
    assert set(u["status"] for u in ups) <= VALID_COMPACTION_STATUSES


@pytest.mark.asyncio
async def test_compaction_cancelled_when_context_untouched(srv: ACPServer):
    """handled=True 但上下文一字未改（历史太短）= cancelled，不伪造 completed。"""
    srv._client_compaction = True
    ctx = _ctx(3)
    await srv._on_compact_start(None, name="/compact", messages=ctx)
    await srv._on_compact_end(None, name="/compact", messages=ctx, handled=True, output="无需压缩")
    assert [u["status"] for u in _updates(srv)] == ["in_progress", "cancelled"]


@pytest.mark.asyncio
async def test_compaction_completed_when_count_unchanged_but_content_rewritten(
    srv: ACPServer,
):
    """条数不变但内容被改写 → 必须 completed。

    compact 在小历史边界上会「1 条早期换成摘要 + 保留 4 条」，条数与原来**相同**。
    只用条数判别会把真压缩误报成 cancelled —— 这正是把判据换成内容指纹的原因。
    """
    srv._client_compaction = True
    before = _ctx(5)
    after = _ctx(4, summary=True)
    assert len(before) == len(after), "构造前提：条数相同"
    await srv._on_compact_start(None, name="/compact", messages=before)
    await srv._on_compact_end(None, name="/compact", messages=after, handled=True, output="✅")
    assert [u["status"] for u in _updates(srv)] == ["in_progress", "completed"]


@pytest.mark.asyncio
async def test_compaction_failed_carries_error_text(srv: ACPServer):
    """handled=False（摘要失败）= failed，并带出可展示原因。"""
    srv._client_compaction = True
    await srv._on_compact_start(None, name="/compact", messages=_ctx(20))
    await srv._on_compact_end(None, name="/compact", messages=_ctx(20), handled=False, output="压缩失败: 429")
    ups = _updates(srv)
    assert [u["status"] for u in ups] == ["in_progress", "failed"]
    assert "429" in ups[1]["error"], "failed 必须带 error，否则客户端只能显示一个空失败"


@pytest.mark.asyncio
async def test_compaction_not_sent_when_client_did_not_declare(srv: ACPServer):
    """规范硬约束：客户端未声明 session.compaction 时 MUST NOT 发任何 compaction_update。"""
    srv._client_compaction = False
    await srv._on_compact_start(None, name="/compact", messages=_ctx(20))
    await srv._on_compact_end(None, name="/compact", messages=_ctx(4, summary=True), handled=True)
    assert _updates(srv) == []


@pytest.mark.asyncio
async def test_compaction_ignores_other_commands(srv: ACPServer):
    """/new 也会改上下文，但它不是压缩：不能借 compaction_update 上报。"""
    srv._client_compaction = True
    await srv._on_compact_start(None, name="/new", messages=_ctx(20))
    await srv._on_compact_end(None, name="/new", messages=_ctx(0), handled=True)
    assert _updates(srv) == []


@pytest.mark.asyncio
async def test_compaction_id_not_reused_across_rounds(srv: ACPServer):
    """两次压缩必须是两个 id（同一 id 二次使用会让客户端把第二次当补丁打到第一条上）。"""
    srv._client_compaction = True
    for _ in range(2):
        await srv._on_compact_start(None, name="/compact", messages=_ctx(20))
        await srv._on_compact_end(None, name="/compact", messages=_ctx(4, summary=True), handled=True)
    ids = [u["compactionId"] for u in _updates(srv)]
    assert len(ids) == 4
    assert len({ids[0], ids[2]}) == 2, "每轮压缩都要新 id"


@pytest.mark.asyncio
async def test_compaction_end_without_start_sends_nothing(srv: ACPServer):
    """没有配对的开态（如客户端中途才声明能力）→ 不发孤立终态。"""
    srv._client_compaction = True
    await srv._on_compact_end(None, name="/compact", messages=_ctx(4), handled=True)
    assert _updates(srv) == []


def test_client_compaction_capability_requires_object(srv: ACPServer):
    """session.compaction 是 Option<结构体>：只有「值是对象」才算声明。"""
    srv._apply_client_capabilities({"session": {"compaction": {}}})
    assert srv._client_compaction is True
    for noise in (None, True, False, "supported", [], 1):
        srv._apply_client_capabilities({"session": {"compaction": noise}})
        assert srv._client_compaction is False, f"{noise!r} 不是结构体，不得当成已声明"


# ── 会话元信息通知（session_info_update）──────────────────


@pytest.mark.asyncio
async def test_session_info_update_sent_when_title_changes(srv: ACPServer, monkeypatch):
    """标题变化 → session_info_update，updatedAt 必须是带时区的 ISO 8601。"""
    fake = _FakeSessions(titles={"sess_test": {"summary": "旧标题"}})
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(fake))
    srv._title_before["sess_test"] = "旧标题"
    fake._titles["sess_test"] = {"summary": "新标题"}
    srv._notify_session_info("sess_test")
    ups = _updates(srv)
    assert len(ups) == 1
    assert ups[0]["sessionUpdate"] == "session_info_update"
    assert ups[0]["title"] == "新标题"
    dt = datetime.datetime.fromisoformat(ups[0]["updatedAt"])
    assert dt.tzinfo is not None, "规范要求带时区，naive 时间会被判非法"


@pytest.mark.asyncio
async def test_session_info_update_silent_when_title_unchanged(srv: ACPServer, monkeypatch):
    """标题没变就不发：每轮都发会让 IDE 线程列表无谓重渲染。"""
    fake = _FakeSessions(titles={"sess_test": {"summary": "同一标题"}})
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(fake))
    srv._title_before["sess_test"] = "同一标题"
    srv._notify_session_info("sess_test")
    assert _updates(srv) == []


@pytest.mark.asyncio
async def test_session_info_update_on_first_turn(srv: ACPServer, monkeypatch):
    """新会话首轮：turn 前无标题，core 派生出来后必须推送。"""
    fake = _FakeSessions(titles={"sess_test": {"summary": "帮我改个 bug"}})
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(fake))
    srv._title_before["sess_test"] = ""  # _run_turn 开头的快照
    srv._notify_session_info("sess_test")
    assert [u["title"] for u in _updates(srv)] == ["帮我改个 bug"]


def test_session_title_reads_any_session_not_just_current(srv: ACPServer, monkeypatch):
    """标题必须能读**非当前**会话：跨会话并发时本轮会话未必是 core 的当前会话。"""
    fake = _FakeSessions(titles={"other": {"summary": "别的会话"}})
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(fake))
    assert srv._session_title("other") == "别的会话"
    assert srv._session_title("unknown") == ""


@pytest.mark.asyncio
async def test_run_turn_snapshots_title_before_prompt(srv: ACPServer, monkeypatch):
    """快照必须取在 turn **之前**，否则一轮跑完再读就与「新标题」比较，永远不推。

    这条守护的是 _run_turn 里那一行 `_title_before[sid] = …`：删掉它，
    session_info_update 会静默失效（首轮尤其明显，新会话标题永远是空的）。
    """
    fake = _FakeSessions(titles={"sess_t": {"summary": "旧标题"}})
    portal_stub = _FakePortal(fake)
    seen: list[str | None] = []

    async def _fake_send(prompt: str, io: Any = None):
        seen.append(srv._title_before.get("sess_t"))
        fake._titles["sess_t"] = {"summary": "新标题"}  # core 在 turn 内派生标题

        class _Resp:
            content = ""

        return _Resp()

    portal_stub.run.send = _fake_send  # type: ignore[attr-defined]
    monkeypatch.setattr(srv_mod, "portal", portal_stub)

    st = srv_mod._SessionState(sid="sess_t", cwd=os.getcwd())
    srv._sessions[st.sid] = st
    srv._active = st
    await srv._run_turn(st, "hi")

    assert seen == ["旧标题"], "turn 开始时就该拿到旧标题快照"
    titles = [u["title"] for u in _updates(srv) if u.get("sessionUpdate") == "session_info_update"]
    assert titles == ["新标题"]


# ── 钩子接线：逻辑对 ≠ 真的被挂上 ─────────────────────────


@pytest.mark.asyncio
async def test_start_registers_compaction_hooks(srv: ACPServer, monkeypatch):
    """`start()` 必须真的把压缩钩子与用量钩子挂进全局生命周期。

    所有压缩用例都直接调 `_on_compact_start/_end`、用量用例都直接调
    `_on_llm_usage`，把 `start()` 里那两句 `_register_*_hooks()` 删掉它们全绿——
    但真实进程里钩子从未注册，compaction_update / usage_update 永远不发。
    与淘汰接线同一类失败：逻辑正确但从未执行。
    """
    registered: list[tuple[Any, str]] = []

    class _Hooks:
        def register(self, hook: Any, fn: Any, name: str = "", priority: int = 0) -> None:
            registered.append((hook, name))

    stub = _FakePortal(_FakeSessions())
    stub.ctl.hooks = _Hooks()  # type: ignore[attr-defined]

    async def _noop_open(**kwargs: Any) -> None:
        return None

    async def _noop_continue() -> None:
        return None

    async def _noop_loop(reader: Any) -> None:
        return None

    async def _noop_shutdown() -> None:
        return None

    stub.ctl.open = _noop_open  # type: ignore[attr-defined]
    stub.run.status.session_id = ""  # 不进入 _adopt_session 分支
    stub.run.status.model = "stub/model"  # type: ignore[attr-defined]
    monkeypatch.setattr(srv_mod, "portal", stub)
    monkeypatch.setattr(srv, "_reload_continue", _noop_continue)
    monkeypatch.setattr(srv, "_read_loop", _noop_loop)
    monkeypatch.setattr(srv, "_shutdown_agent", _noop_shutdown)
    # start() 会 connect_read_pipe(sys.stdin)：pytest 的假 stdin 没有真实 fd
    r, w = os.pipe()
    monkeypatch.setattr(srv_mod.sys, "stdin", io.TextIOWrapper(os.fdopen(r, "rb")))
    try:
        await srv.start()
    finally:
        os.close(w)

    names = {name for _, name in registered}
    assert {"acp_compaction_start", "acp_compaction_end"} <= names, f"压缩钩子未接线，实际注册: {sorted(names)}"
    hooks = {hook: name for hook, name in registered}
    assert hooks.get(srv_mod.LifecycleHook.ON_BEFORE_COMMAND) == "acp_compaction_start"
    assert hooks.get(srv_mod.LifecycleHook.ON_COMMAND) == "acp_compaction_end"
    # 用量钩子同理：删掉 `_register_usage_hook()` 后，所有 usage 用例仍绿，
    # 而真实进程里 `_usage_used` 永远是空的 → usage_update 一次都不发。
    assert hooks.get(srv_mod.LifecycleHook.ON_AFTER_LLM_CALL) == "acp_usage_capture"


# ── follow-along：ToolCallLocation.line（1-based） ──────────
#
# ToolCallLocation = {_meta?, line?: u32(1-based), path: string(绝对路径)}
# 只发 path 时 IDE 能打开文件但落点靠猜；line 才是「跟随 Agent」真正有用的那一半。


def _write(tmp_path: Any, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


@pytest.mark.asyncio
async def test_read_file_location_line_from_offset(srv: ACPServer, tmp_path):
    """read_file 的 offset 是 0-based，ToolCallLocation.line 是 1-based。"""
    path = _write(tmp_path, "a.py", "l1\nl2\nl3\nl4\nl5\n")
    await srv._on_tool_call(
        None, tool_name="read_file", tool_args=json.dumps({"file_path": path, "offset": 2}), tool_call_id="c1"
    )
    (upd,) = _updates(srv)
    (loc,) = upd["locations"]
    assert loc["path"] == path
    assert loc["line"] == 3, "offset=2(0-based) 应换算为 line=3(1-based)"


@pytest.mark.asyncio
async def test_read_file_location_omits_line_without_offset(srv: ACPServer, tmp_path):
    """没给 offset = 从文件头读，line 应为 1（offset 缺省的语义就是 0）。"""
    path = _write(tmp_path, "b.py", "x\ny\n")
    await srv._on_tool_call(None, tool_name="read_file", tool_args=json.dumps({"file_path": path}), tool_call_id="c1")
    (upd,) = _updates(srv)
    (loc,) = upd["locations"]
    assert loc["path"] == path
    assert "line" not in loc, "offset 缺省时不必塞 line（编辑器默认打开即首行）"


@pytest.mark.asyncio
async def test_edit_file_location_line_locates_old_string(srv: ACPServer, tmp_path):
    """edit_file 只给 old_string/new_string，落点必须按文件内容定位（编辑前）。"""
    path = _write(tmp_path, "c.py", "alpha\nbeta\ngamma\n")
    await srv._on_tool_call(
        None,
        tool_name="edit_file",
        tool_args=json.dumps({"file_path": path, "old_string": "gamma", "new_string": "delta"}),
        tool_call_id="c1",
    )
    (upd,) = _updates(srv)
    (loc,) = upd["locations"]
    assert loc["line"] == 3


@pytest.mark.asyncio
async def test_edit_file_location_multiline_needle(srv: ACPServer, tmp_path):
    """old_string 可能是多行片段 → 用首次出现的起始行，而不是逐行比对。"""
    path = _write(tmp_path, "d.py", "a\nb\nc\nd\ne\n")
    await srv._on_tool_call(
        None,
        tool_name="edit_file",
        tool_args=json.dumps({"file_path": path, "old_string": "c\nd", "new_string": "X"}),
        tool_call_id="c1",
    )
    (upd,) = _updates(srv)
    assert upd["locations"][0]["line"] == 3


@pytest.mark.asyncio
async def test_edit_file_location_missing_needle_sends_path_only(srv: ACPServer, tmp_path):
    """定位失败绝不能拖垮工具调用：退化成只发 path，locations 仍然存在。"""
    path = _write(tmp_path, "e.py", "one\ntwo\n")
    await srv._on_tool_call(
        None,
        tool_name="edit_file",
        tool_args=json.dumps({"file_path": path, "old_string": "NOT_THERE", "new_string": "x"}),
        tool_call_id="c1",
    )
    (upd,) = _updates(srv)
    (loc,) = upd["locations"]
    assert loc["path"] == path
    assert "line" not in loc


@pytest.mark.asyncio
async def test_write_file_location_has_no_line(srv: ACPServer, tmp_path):
    """write_file 整篇覆盖，没有「落到哪一行」的语义 —— 只给 path。"""
    path = _write(tmp_path, "f.py", "z\n")
    await srv._on_tool_call(
        None, tool_name="write_file", tool_args=json.dumps({"file_path": path, "content": "q"}), tool_call_id="c1"
    )
    (upd,) = _updates(srv)
    (loc,) = upd["locations"]
    assert "line" not in loc


@pytest.mark.asyncio
async def test_tool_call_update_omits_locations_after_edit(srv: ACPServer, tmp_path):
    """tool_call_update 省略 locations = 保持不变（绝不自作聪明重算，会带偏光标）。"""
    path = _write(tmp_path, "g.py", "old\ntail\n")
    await srv._on_tool_call(
        None,
        tool_name="edit_file",
        tool_args=json.dumps({"file_path": path, "old_string": "old", "new_string": "new"}),
        tool_call_id="c1",
    )
    _updates(srv)
    await srv._on_tool_result(None, tool_name="edit_file", result="done", tool_call_id="c1")
    (upd,) = _updates(srv)
    assert "locations" not in upd, "省略=保持不变；发 [] 会清空，发重算的行号可能错"


@pytest.mark.asyncio
async def test_tool_call_update_adds_location_for_newly_created_file(srv: ACPServer, tmp_path):
    """新建文件：call 阶段文件不存在（无 locations），完成后补一条可 follow 的位置。"""
    path = str(tmp_path / "brand_new.py")
    await srv._on_tool_call(
        None, tool_name="write_file", tool_args=json.dumps({"file_path": path, "content": "hi"}), tool_call_id="c1"
    )
    (call_upd,) = _updates(srv)
    assert "locations" not in call_upd, "文件还不存在时不该发 follow 位置"

    (tmp_path / "brand_new.py").write_text("hi", encoding="utf-8")
    await srv._on_tool_result(None, tool_name="write_file", result="done", tool_call_id="c1")
    (upd,) = _updates(srv)
    (loc,) = upd["locations"]
    assert loc["path"] == path
    assert "line" not in loc


@pytest.mark.asyncio
async def test_tool_call_update_no_location_for_failed_write(srv: ACPServer, tmp_path):
    """写失败时文件可能半成品/不存在 —— 不发 locations。"""
    path = str(tmp_path / "fail.py")
    await srv._on_tool_call(
        None, tool_name="write_file", tool_args=json.dumps({"file_path": path, "content": "hi"}), tool_call_id="c1"
    )
    _updates(srv)
    await srv._on_tool_result(None, tool_name="write_file", result="❌ 写入失败", tool_call_id="c1")
    (upd,) = _updates(srv)
    assert upd["status"] == "failed"
    assert "locations" not in upd


# ── agent_thought_chunk：让 IDE 显示推理过程 ──────────────────
#
# ACP v1 ContentChunk = {content: ContentBlock, messageId?: string}
# sessionUpdate 取值 "agent_thought_chunk"。core 的 stream_think → 这里；
# 此前 ACPIO 没实现 stream_think，推理 token 全部静默丢弃。


@pytest.mark.asyncio
async def test_thought_chunk_shape_and_message_id(srv: ACPServer):
    """思考 token → agent_thought_chunk；同段思考共用一个 messageId。"""
    acp_io = srv._make_acp_io("sess_test")
    await acp_io.thinking_start(streaming=True)
    acp_io.stream_think("先看")
    acp_io.stream_think("一下目录")
    await acp_io.thinking_stop()

    (upd,) = _updates(srv)
    assert upd["sessionUpdate"] == "agent_thought_chunk"
    assert upd["content"] == {"type": "text", "text": "先看一下目录"}, "同段思考应合批，而不是一 token 一条"
    assert upd["messageId"].startswith("tht_")

    # 第二段思考 → messageId 必须变（客户端据此分块）
    await acp_io.thinking_start(streaming=True)
    acp_io.stream_think("再想想")
    await acp_io.thinking_stop()
    (upd2,) = _updates(srv)
    assert upd2["messageId"] != upd["messageId"]


@pytest.mark.asyncio
async def test_thought_is_separate_from_message_channel(srv: ACPServer):
    """推理绝不能漏进正文（agent_message_chunk），否则内容重复且语义错。"""
    acp_io = srv._make_acp_io("sess_test")
    await acp_io.thinking_start(streaming=True)
    acp_io.stream_think("内部推理")
    await acp_io.thinking_stop()
    acp_io.say("给用户的回答")
    leftover = acp_io.flush_text()

    updates = _updates(srv)
    kinds = [u["sessionUpdate"] for u in updates]
    assert kinds == ["agent_thought_chunk"]
    assert updates[0]["content"]["text"] == "内部推理"
    assert leftover == "给用户的回答", "正文缓冲不应被思考污染"


@pytest.mark.asyncio
async def test_thought_flushed_before_final_text(srv: ACPServer):
    """思考必须排在最终回答之前 —— 顺序颠倒会让客户端把推理显示在回答之后。"""
    acp_io = srv._make_acp_io("sess_test")
    await acp_io.thinking_start(streaming=True)
    acp_io.stream_think("想好了")
    acp_io.say("结果")  # 未达阈值，仍在缓冲
    acp_io.flush_text()

    updates = _updates(srv)
    assert [u["sessionUpdate"] for u in updates] == ["agent_thought_chunk"]


@pytest.mark.asyncio
async def test_thought_not_sent_when_empty(srv: ACPServer):
    """没有推理的轮次不得凭空长出空思考块。"""
    acp_io = srv._make_acp_io("sess_test")
    await acp_io.thinking_start(streaming=True)
    await acp_io.thinking_stop()
    acp_io.flush_text()
    assert _updates(srv) == []


@pytest.mark.asyncio
async def test_thought_empty_token_ignored(srv: ACPServer):
    """空 token 不入缓冲（否则会推送 text="" 的无效 ContentBlock）。"""
    acp_io = srv._make_acp_io("sess_test")
    await acp_io.thinking_start(streaming=True)
    acp_io.stream_think("")
    await acp_io.thinking_stop()
    assert _updates(srv) == []


@pytest.mark.asyncio
async def test_thought_uses_per_turn_session_id(srv: ACPServer):
    """思考块必须带当前会话 id（多会话并存时不能串到别的会话）。"""
    acp_io = srv._make_acp_io("sess_other")
    await acp_io.thinking_start(streaming=True)
    acp_io.stream_think("x")
    await acp_io.thinking_stop()
    (msg,) = _messages(srv)
    assert msg["params"]["sessionId"] == "sess_other"


@pytest.mark.asyncio
async def test_thought_threshold_batches_long_stream(srv: ACPServer):
    """超过阈值立即成批推送（不等 thinking_stop），否则长思考在 IDE 里一直空白。"""
    acp_io = srv._make_acp_io("sess_test")
    await acp_io.thinking_start(streaming=True)
    chunk = "字" * (acp_io._STREAM_THRESHOLD // 2)
    acp_io.stream_think(chunk)
    assert _updates(srv) == [], "未达阈值不应推送"
    acp_io.stream_think(chunk)
    (upd,) = _updates(srv)
    assert len(upd["content"]["text"]) >= acp_io._STREAM_THRESHOLD


@pytest.mark.asyncio
async def test_thought_dropped_without_callback():
    """未注入 send_thought 时静默丢弃（旧行为），不得抛错。"""
    acp_io = srv_mod.ACPIO(send_chunk=None)
    await acp_io.thinking_start(streaming=True)
    acp_io.stream_think("x")
    await acp_io.thinking_stop()
    assert acp_io.flush_text() == ""


# ── configOptions：Zed 里选模型的唯一通道 ─────────────────────
#
# SessionConfigOption(select) = {id, name, description?, category?, type:"select",
#                                currentValue, options:[{value, name, description?}]}
# category 枚举（schema，snake_case）：mode / model / model_config / thought_level
VALID_CONFIG_CATEGORIES = {"mode", "model", "model_config", "thought_level"}


@pytest.fixture
def llm_cfg(tmp_path: Any, monkeypatch: Any) -> Any:
    """把 LLM 配置表与内存态钉到临时值（不读写用户真实配置）。

    `_model_catalog` 读 `USER_CONFIG_PATH` 文件，而 `_current_model_id` /
    `_context_window_tokens` 读 config 模块的**内存态** —— 只钉路径不够，
    内存态不钉则断言会随本机 ACTIVE_LLM / MAX_CONTEXT_TOKENS 漂移。
    """
    from fp_core import config as cfg

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({
            "LLM_PROVIDERS": {
                # p1/m1 与 p2/m1 同名不同 provider —— value 必须是 provider/model，
                # 否则 Zed 的下拉框里两项同值，选中会互相覆盖。
                "p1": {"models": {"m1": {}, "m2": {}}},
                "p2": {"models": {"m1": {}}},
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", str(path))
    monkeypatch.setattr(cfg, "LLM_ACTIVE_ID", "p1/m1")
    monkeypatch.setattr(cfg, "MAX_CONTEXT_TOKENS", 8000)
    return cfg


@pytest.mark.asyncio
async def test_session_new_response_carries_config_options(
    srv: ACPServer, monkeypatch: Any, llm_cfg: Any, tmp_path: Any
):
    """session/new 的响应必须带 configOptions —— 缺了它 Zed 会话头部没有模型下拉框。"""
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions()))
    result = await srv._handle_session_new({"cwd": str(tmp_path)})

    (opt,) = result["configOptions"]
    assert opt["id"] == "model"
    assert opt["type"] == "select"
    assert opt["category"] in VALID_CONFIG_CATEGORIES
    assert opt["currentValue"] == "p1/m1"
    values = {o["value"] for o in opt["options"]}
    assert values == {"p1/m1", "p1/m2", "p2/m1"}, "模型同名跨 provider，value 必须带 provider"
    assert opt["currentValue"] in values, "currentValue 必须是可选中的值"


@pytest.mark.asyncio
async def test_session_new_omits_config_options_without_catalog(srv: ACPServer, monkeypatch: Any, tmp_path: Any):
    """无配置表 → **整字段省略**。

    空数组在客户端语义是「有配置但没有选项」（会渲染成空下拉框），比不报更糟。
    """
    from fp_core import config as cfg

    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions()))
    result = await srv._handle_session_new({"cwd": str(tmp_path)})
    assert "configOptions" not in result


def test_config_options_appends_active_model_outside_catalog(llm_cfg: Any):
    """激活项不在配置表（--model / FP_MODEL 覆盖到表外模型）时必须补一项。

    否则 `currentValue` 指向一个不存在的选项，Zed 显示成空选择 ——
    用户以为「没有模型可用」，实际是在用的。
    """
    (opt,) = srv_mod._config_options(srv_mod._model_catalog(), "override/xyz")
    values = [o["value"] for o in opt["options"]]
    assert "override/xyz" in values
    assert opt["currentValue"] == "override/xyz"


@pytest.mark.asyncio
async def test_set_config_option_rejects_unknown_config_id(srv: ACPServer, monkeypatch: Any, llm_cfg: Any):
    """本端只有 model；thought_level/mode 一律 -32602（fp 没有这些可调面）。"""
    portal_stub = _FakePortal(_FakeSessions())
    monkeypatch.setattr(srv_mod, "portal", portal_stub)
    srv._sessions["sess_test"] = srv_mod._SessionState(sid="sess_test", cwd="/tmp")

    for config_id in ("thought_level", "mode", None):
        with pytest.raises(_ACPRPCError) as ei:
            await srv._handle_session_set_config_option({
                "sessionId": "sess_test",
                "configId": config_id,
                "value": "p2/m1",
            })
        assert ei.value.code == -32602
    assert portal_stub.run.command_calls == [], "参数非法时不得执行任何命令"


@pytest.mark.asyncio
async def test_set_config_option_rejects_non_string_value(srv: ACPServer, monkeypatch: Any, llm_cfg: Any):
    """select 变体的 value 是字符串；布尔变体的对象形态本端不提供，明确拒绝而非强转。

    断言**报错文案**而非只断言错误码：非法形态与「未知模型」都是 -32602，
    但把 `{"type":"boolean",...}` 报成「未知模型: {'type': …}」会让排查者
    以为模型名写错了。两者要能区分得开，这条测试才有意义。
    """
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions()))
    srv._sessions["sess_test"] = srv_mod._SessionState(sid="sess_test", cwd="/tmp")
    for bad in ({"type": "boolean", "value": True}, "", None, 1):
        with pytest.raises(_ACPRPCError) as ei:
            await srv._handle_session_set_config_option({"sessionId": "sess_test", "configId": "model", "value": bad})
        assert ei.value.code == -32602
        assert "选项 id 字符串" in ei.value.message, f"{bad!r} 被误报成模型名问题: {ei.value.message}"


@pytest.mark.asyncio
async def test_set_config_option_rejects_unknown_model(srv: ACPServer, monkeypatch: Any, llm_cfg: Any):
    """未知模型不得「挑一个最像的」执行 —— 静默选错模型会污染用户钱包。"""
    portal_stub = _FakePortal(_FakeSessions(), commands={"model": object()})
    monkeypatch.setattr(srv_mod, "portal", portal_stub)
    srv._sessions["sess_test"] = srv_mod._SessionState(sid="sess_test", cwd="/tmp")
    with pytest.raises(_ACPRPCError) as ei:
        await srv._handle_session_set_config_option({"sessionId": "sess_test", "configId": "model", "value": "p9/nope"})
    assert ei.value.code == -32602
    assert portal_stub.run.command_calls == []


@pytest.mark.asyncio
async def test_set_config_option_requires_model_command(srv: ACPServer, monkeypatch: Any, llm_cfg: Any):
    """切换语义只在 /model 扩展里；扩展缺失时**明确报错**，不能让用户以为切成功了。

    断言「压根没尝试执行」而不只是「报错了」：否则与
    `test_set_config_option_verifies_observable_state_not_output_text` 撞成同一症状
    （命令跑了、状态没变），这条就杀不掉「缺命令时仍然硬执行」的变异。
    """
    portal_stub = _FakePortal(_FakeSessions(), commands={})
    monkeypatch.setattr(srv_mod, "portal", portal_stub)
    srv._sessions["sess_test"] = srv_mod._SessionState(sid="sess_test", cwd="/tmp")
    with pytest.raises(_ACPRPCError) as ei:
        await srv._handle_session_set_config_option({"sessionId": "sess_test", "configId": "model", "value": "p2/m1"})
    assert ei.value.code == -32603
    assert "未安装" in ei.value.message, "要让用户知道是扩展缺失，而不是模型切换失败"
    assert portal_stub.run.command_calls == [], "未安装时不得凭空调用命令层"
    assert llm_cfg.LLM_ACTIVE_ID == "p1/m1", "失败路径不得改动激活模型"


@pytest.mark.asyncio
async def test_set_config_option_delegates_to_model_command(srv: ACPServer, monkeypatch: Any, llm_cfg: Any):
    """切换委托命令层（唯一的真相），响应回**完整** configOptions。"""

    def _on_command(text: str) -> tuple[bool, str]:
        llm_cfg.LLM_ACTIVE_ID = text.split()[1]
        return True, "已切换"

    portal_stub = _FakePortal(_FakeSessions(), commands={"model": object()}, on_command=_on_command)
    monkeypatch.setattr(srv_mod, "portal", portal_stub)
    srv._sessions["sess_test"] = srv_mod._SessionState(sid="sess_test", cwd="/tmp")

    result = await srv._handle_session_set_config_option({
        "sessionId": "sess_test",
        "configId": "model",
        "value": "p2/m1",
    })
    assert portal_stub.run.command_calls == ["/model p2/m1"], "必须走命令层，不得自己实现一份"
    assert result["configOptions"][0]["currentValue"] == "p2/m1"


@pytest.mark.asyncio
async def test_set_config_option_verifies_observable_state_not_output_text(
    srv: ACPServer, monkeypatch: Any, llm_cfg: Any
):
    """命令「说成功了」但状态没变 → 必须报错。

    守护的是「用可观测状态验收，而不是解析命令输出的文案」：文案会随扩展漂移，
    而 config 内存态是唯一真源。
    """
    portal_stub = _FakePortal(
        _FakeSessions(),
        commands={"model": object()},
        on_command=lambda _text: (True, "✅ 已切换到 p2/m1"),  # 只改文案，不改状态
    )
    monkeypatch.setattr(srv_mod, "portal", portal_stub)
    srv._sessions["sess_test"] = srv_mod._SessionState(sid="sess_test", cwd="/tmp")

    with pytest.raises(_ACPRPCError) as ei:
        await srv._handle_session_set_config_option({"sessionId": "sess_test", "configId": "model", "value": "p2/m1"})
    assert ei.value.code == -32603
    assert "p1/m1" in ei.value.message, "报错要带出真实的当前模型，否则用户无从判断"


@pytest.mark.asyncio
async def test_set_config_option_same_value_skips_command(srv: ACPServer, monkeypatch: Any, llm_cfg: Any):
    """选中的就是当前模型 → 不执行命令（无谓的 client 重建会打断正在跑的东西）。"""
    portal_stub = _FakePortal(_FakeSessions(), commands={"model": object()})
    monkeypatch.setattr(srv_mod, "portal", portal_stub)
    srv._sessions["sess_test"] = srv_mod._SessionState(sid="sess_test", cwd="/tmp")

    result = await srv._handle_session_set_config_option({
        "sessionId": "sess_test",
        "configId": "model",
        "value": "p1/m1",
    })
    assert portal_stub.run.command_calls == []
    assert result["configOptions"][0]["currentValue"] == "p1/m1"


@pytest.mark.asyncio
async def test_set_config_option_broadcasts_to_all_sessions(srv: ACPServer, monkeypatch: Any, llm_cfg: Any):
    """模型是**进程级单例**：所有已登记会话的下拉框都要跟着更新，否则别的 thread 显示旧值。"""

    def _on_command(text: str) -> tuple[bool, str]:
        llm_cfg.LLM_ACTIVE_ID = text.split()[1]
        return True, ""

    portal_stub = _FakePortal(_FakeSessions(), commands={"model": object()}, on_command=_on_command)
    monkeypatch.setattr(srv_mod, "portal", portal_stub)
    srv._sessions.clear()
    for sid in ("sess_a", "sess_b"):
        srv._sessions[sid] = srv_mod._SessionState(sid=sid, cwd="/tmp")
    srv._session_id = "sess_a"

    await srv._handle_session_set_config_option({"sessionId": "sess_a", "configId": "model", "value": "p2/m1"})
    pushes = [
        (m["params"]["sessionId"], m["params"]["update"]) for m in _messages(srv) if m.get("method") == "session/update"
    ]
    targets = {sid for sid, upd in pushes if upd["sessionUpdate"] == "config_option_update"}
    assert targets == {"sess_a", "sess_b"}, f"广播漏了会话: {targets}"
    for _sid, upd in pushes:
        assert upd["configOptions"], "config_option_update 必须带完整选项（客户端就地替换）"


@pytest.mark.asyncio
async def test_set_config_option_requires_known_session(srv: ACPServer, monkeypatch: Any, llm_cfg: Any):
    """会话 id 地址无效 → -32602（不能默默改一个不属于任何 thread 的进程级设置）。"""
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions()))
    with pytest.raises(_ACPRPCError) as ei:
        await srv._handle_session_set_config_option({"configId": "model", "value": "p1/m1"})
    assert ei.value.code == -32602


@pytest.mark.asyncio
async def test_set_config_option_reachable_via_dispatch(srv: ACPServer, monkeypatch: Any, llm_cfg: Any):
    """必须真的挂进 dispatch 表：漏一行 handler 就是死代码，客户端一调 -32601。"""

    def _on_command(text: str) -> tuple[bool, str]:
        llm_cfg.LLM_ACTIVE_ID = text.split()[1]
        return True, ""

    monkeypatch.setattr(
        srv_mod, "portal", _FakePortal(_FakeSessions(), commands={"model": object()}, on_command=_on_command)
    )
    srv._sessions["sess_test"] = srv_mod._SessionState(sid="sess_test", cwd="/tmp")
    await srv._dispatch({
        "jsonrpc": "2.0",
        "id": 21,
        "method": "session/set_config_option",
        "params": {"sessionId": "sess_test", "configId": "model", "value": "p2/m1"},
    })
    (msg,) = [m for m in _messages(srv) if m.get("id") == 21]
    assert "error" not in msg, f"未接线 → {msg.get('error')}"
    assert msg["result"]["configOptions"][0]["currentValue"] == "p2/m1"


# ── session/resume（只挂不重放）+ session/delete（连盘一起删）──


@pytest.mark.asyncio
async def test_resume_and_delete_advertised(srv: ACPServer, monkeypatch: Any):
    """声明了才允许客户端调用：sessionCapabilities.resume / delete。"""
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions()))
    monkeypatch.setattr(srv, "_send_available_commands", lambda: None)
    caps = await srv._handle_initialize({"protocolVersion": 1})
    sess_caps = caps["agentCapabilities"]["sessionCapabilities"]
    assert {"resume", "delete"} <= set(sess_caps)


@pytest.mark.asyncio
async def test_session_resume_does_not_replay_history(srv: ACPServer, monkeypatch: Any, tmp_path: Any):
    """resume 与 load 的唯一差别就是**不回放**（schema 原文）—— 大历史切 thread 靠它。"""
    history = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "在的"}]
    fake = _FakeSessions(history=history, list_result={"sess_old": {"cwd": str(tmp_path)}})
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(fake))

    result = await srv._handle_session_resume({"sessionId": "sess_old", "cwd": str(tmp_path)})
    assert result["sessionId"] == "sess_old"
    assert _updates(srv) == [], "resume 不得回放历史（那是 session/load 的职责）"


@pytest.mark.asyncio
async def test_session_resume_rejects_unknown_session(srv: ACPServer, monkeypatch: Any, tmp_path: Any):
    """「快」不能以「不校验」为代价：未知会话 → -32602，而不是静默新建空会话。"""
    fake = _FakeSessions(history=None)  # 盘上无此会话
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(fake))
    with pytest.raises(_ACPRPCError) as ei:
        await srv._handle_session_resume({"sessionId": "ghost", "cwd": str(tmp_path)})
    assert ei.value.code == -32602


def test_adopt_session_refuses_unprovable_session(srv: ACPServer, monkeypatch: Any):
    """**三条证明都不成立**时不收养（否则会给一个陌生 sid 凭空造会话）。

    这条走的是 `_adopt_session` 自己的兜底分支：load/resume 总会带 cwd（协议必填），
    所以它只有 `start()` / reload 恢复那条路径会走到 —— 那里 cwd 是未知的，
    只认「本次连接登记过 / 盘上有历史 / 就是 core 当前会话」。
    """
    fake = _FakeSessions(history=None, list_result={})
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(fake))

    assert srv._adopt_session("ghost") is None
    assert "ghost" not in srv._sessions, "陌生 sid 不得被写进会话表"

    # 反向控制：core 当前会话属于第三条证明，必须能收养
    st = srv._adopt_session("sess_test")
    assert st is not None and st.sid == "sess_test"


@pytest.mark.asyncio
async def test_session_resume_makes_session_addressable(srv: ACPServer, monkeypatch: Any, tmp_path: Any):
    """挂上之后必须能按 sid 路由，否则下一次 prompt 还是 -32602。

    同时断言「只登记、不切 core」：core 当前会话仍是原来那条（否则 load/resume A
    会把正在输出的 B 顶掉）。
    """
    fake = _FakeSessions(history=[], list_result={"sess_old": {"cwd": str(tmp_path)}})
    portal_stub = _FakePortal(fake)
    monkeypatch.setattr(srv_mod, "portal", portal_stub)

    await srv._handle_session_resume({"sessionId": "sess_old", "cwd": str(tmp_path)})

    assert "sess_old" in srv._sessions
    assert srv._session_id == "sess_old"
    assert srv._resolve_session({"sessionId": "sess_old"}, "session/prompt").cwd == str(tmp_path)
    assert portal_stub.run.status.session_id == "sess_test", "resume 不得改写 core 当前会话"


@pytest.mark.asyncio
async def test_session_resume_returns_config_options(srv: ACPServer, monkeypatch: Any, llm_cfg: Any, tmp_path: Any):
    """resume 也是「建立会话」，同样要带 configOptions（否则切回来的 thread 没有下拉框）。"""
    fake = _FakeSessions(history=[], list_result={"sess_old": {"cwd": str(tmp_path)}})
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(fake))
    result = await srv._handle_session_resume({"sessionId": "sess_old", "cwd": str(tmp_path)})
    assert result["configOptions"][0]["currentValue"] == "p1/m1"


@pytest.mark.asyncio
async def test_session_resume_reachable_via_dispatch(srv: ACPServer, monkeypatch: Any, tmp_path: Any):
    fake = _FakeSessions(history=[], list_result={"sess_old": {"cwd": str(tmp_path)}})
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(fake))
    await srv._dispatch({
        "jsonrpc": "2.0",
        "id": 22,
        "method": "session/resume",
        "params": {"sessionId": "sess_old", "cwd": str(tmp_path)},
    })
    (msg,) = [m for m in _messages(srv) if m.get("id") == 22]
    assert "error" not in msg, f"未接线 → {msg.get('error')}"
    assert msg["result"]["sessionId"] == "sess_old"


@pytest.mark.asyncio
async def test_session_delete_removes_disk_and_connection_state(srv: ACPServer, monkeypatch: Any):
    """delete = 连磁盘历史一起删（与 close 的分界线），且不能留下吃资源的子进程。"""
    plug = _FakeMcpPlugin()
    fake = _FakeSessions()
    fake.deletable.add("sess_old")
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(fake, _FakePlugins({"mcp": plug})))
    srv._sessions["sess_old"] = srv_mod._SessionState(sid="sess_old", cwd="/tmp")

    assert await srv._handle_session_delete({"sessionId": "sess_old"}) == {}
    assert fake.deleted == ["sess_old"], "delete 必须删磁盘历史"
    assert "sess_old" not in srv._sessions
    assert plug.dropped == ["sess_old"], "删掉的会话不能继续占着 MCP 子进程"


@pytest.mark.asyncio
async def test_session_delete_refuses_current_open_session(srv: ACPServer, monkeypatch: Any):
    """删当前打开的会话 → core 仍持有它，下次保存会把文件写回来（thread「复活」）。

    用户看到「删了又回来了」比报错更困惑，所以必须明确拒绝。
    """
    fake = _FakeSessions()
    fake.deletable.add("sess_test")
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(fake))
    with pytest.raises(_ACPRPCError) as ei:
        await srv._handle_session_delete({"sessionId": "sess_test"})
    assert ei.value.code == -32600
    assert fake.deleted == [], "先删再报错等于数据已毁"


@pytest.mark.asyncio
async def test_session_delete_unknown_is_idempotent(srv: ACPServer, monkeypatch: Any):
    """删一个本就不存在的会话不报错（客户端重试/多路删除都合法）。"""
    fake = _FakeSessions()  # deletable 为空 → delete() 返回 False
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(fake))
    assert await srv._handle_session_delete({"sessionId": "ghost"}) == {}
    assert fake.deleted == ["ghost"]


@pytest.mark.asyncio
async def test_session_delete_requires_session_id(srv: ACPServer, monkeypatch: Any):
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(_FakeSessions()))
    for params in ({}, {"sessionId": ""}, {"sessionId": 123}):
        with pytest.raises(_ACPRPCError) as ei:
            await srv._handle_session_delete(params)
        assert ei.value.code == -32602


@pytest.mark.asyncio
async def test_session_delete_reachable_via_dispatch(srv: ACPServer, monkeypatch: Any):
    fake = _FakeSessions()
    fake.deletable.add("sess_old")
    monkeypatch.setattr(srv_mod, "portal", _FakePortal(fake))
    await srv._dispatch({
        "jsonrpc": "2.0",
        "id": 23,
        "method": "session/delete",
        "params": {"sessionId": "sess_old"},
    })
    (msg,) = [m for m in _messages(srv) if m.get("id") == 23]
    assert "error" not in msg, f"未接线 → {msg.get('error')}"
    assert fake.deleted == ["sess_old"]


# ── usage_update：上下文占用 ──────────────────────────
#
# UsageUpdate = {used: u64, size: u64, cost?: {amount, currency}}
# used/size 的单位由协议约定为 token。


@pytest.mark.asyncio
async def test_usage_update_shape(srv: ACPServer, llm_cfg: Any):
    srv._usage_update(1234, session_id="sess_test")
    (upd,) = _updates(srv)
    assert upd["sessionUpdate"] == "usage_update"
    assert upd["used"] == 1234
    assert upd["size"] == 8000, "size 用 fp 的压缩阈值（MAX_CONTEXT_TOKENS），不是模型真实窗口"
    assert "cost" not in upd, "fp 没有价格表，编一个数比不发更糟"


@pytest.mark.asyncio
async def test_usage_update_size_never_below_used(srv: ACPServer, llm_cfg: Any):
    """上下文已超预算（即将压缩）时 size 必须 >= used，否则客户端进度条 >100%。"""
    srv._usage_update(99999, session_id="sess_test")
    (upd,) = _updates(srv)
    assert upd["size"] >= upd["used"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tokens", [0, -1])
async def test_usage_update_skipped_without_measurement(srv: ACPServer, llm_cfg: Any, tokens: int):
    """没有测量值不发：used=0 在客户端是「上下文空着」，是假话。"""
    srv._usage_update(tokens, session_id="sess_test")
    assert _updates(srv) == []


@pytest.mark.asyncio
async def test_llm_usage_recorded_only_inside_turn(srv: ACPServer):
    """turn 之外的隐性调用（标题派生 / subagent）不属于任何一轮，不能挂到 thread 上。"""
    srv._active = None
    await srv._on_llm_usage(None, usage={"prompt_tokens": 500})
    assert srv._usage_used == {}

    srv._active = srv_mod._SessionState(sid="sess_t", cwd="/tmp")
    await srv._on_llm_usage(None, usage={"prompt_tokens": 500})
    assert srv._usage_used == {"sess_t": 500}


@pytest.mark.asyncio
async def test_llm_usage_ignores_missing_or_zero_usage(srv: ACPServer):
    """后端不回 usage → 不记录（不能拿 0 或别的会话的值顶上）。"""
    srv._active = srv_mod._SessionState(sid="sess_t", cwd="/tmp")
    await srv._on_llm_usage(None)
    await srv._on_llm_usage(None, usage=None)
    await srv._on_llm_usage(None, usage={"prompt_tokens": 0})
    await srv._on_llm_usage(None, usage={"prompt_tokens": "123"})
    assert srv._usage_used == {}


def _turn_stub(
    srv: ACPServer,
    fake: _FakeSessions,
    *,
    on_send: Any,
) -> _FakePortal:
    """构造一拍即合的 run.send 桩（按需在「轮内」触发用量钩子）。"""
    portal_stub = _FakePortal(fake)
    portal_stub.run.reset_cancelled = lambda: None  # type: ignore[attr-defined]

    async def _send(prompt: str, io: Any = None) -> Any:
        return await on_send()

    portal_stub.run.send = _send  # type: ignore[attr-defined]
    st = srv_mod._SessionState(sid="sess_t", cwd=os.getcwd())
    srv._sessions[st.sid] = st
    srv._active = st
    return portal_stub


@pytest.mark.asyncio
async def test_run_turn_emits_usage_after_reply(srv: ACPServer, monkeypatch: Any, llm_cfg: Any):
    """一轮跑完必须报用量（钩子记账 → turn 末尾统一推送）。"""

    async def _on_send() -> Any:
        await srv._on_llm_usage(None, usage={"prompt_tokens": 3210})

        class _Resp:
            content = "答"

        return _Resp()

    monkeypatch.setattr(srv_mod, "portal", _turn_stub(srv, _FakeSessions(), on_send=_on_send))
    st = srv._sessions["sess_t"]
    await srv._run_turn(st, "hi")

    usage = [u for u in _updates(srv) if u.get("sessionUpdate") == "usage_update"]
    assert len(usage) == 1
    assert usage[0]["used"] == 3210
    assert usage[0]["size"] >= 3210


@pytest.mark.asyncio
async def test_run_turn_clears_stale_usage_between_turns(srv: ACPServer, monkeypatch: Any, llm_cfg: Any):
    """纯命令轮次没调 LLM → **不发**用量（上一轮的数字不能冒充本轮）。"""

    class _Resp:
        content = ""

    async def _on_send() -> Any:
        return _Resp()

    srv._usage_used["sess_t"] = 9999  # 上一轮残留
    monkeypatch.setattr(srv_mod, "portal", _turn_stub(srv, _FakeSessions(), on_send=_on_send))
    st = srv._sessions["sess_t"]
    await srv._run_turn(st, "/compact")

    assert [u for u in _updates(srv) if u.get("sessionUpdate") == "usage_update"] == []


@pytest.mark.asyncio
async def test_run_turn_emits_usage_on_cancel(srv: ACPServer, monkeypatch: Any, llm_cfg: Any):
    """用户中断时上下文已经涨了，占用数字得跟上（cancelled 分支也要报）。"""

    async def _on_send() -> Any:
        await srv._on_llm_usage(None, usage={"prompt_tokens": 2048})
        raise asyncio.CancelledError

    monkeypatch.setattr(srv_mod, "portal", _turn_stub(srv, _FakeSessions(), on_send=_on_send))
    st = srv._sessions["sess_t"]
    result = await srv._run_turn(st, "hi")

    assert result["stopReason"] == "cancelled"
    usage = [u for u in _updates(srv) if u.get("sessionUpdate") == "usage_update"]
    assert [u["used"] for u in usage] == [2048]


# ── notice：结构化告警（门控 clientCapabilities.session.notices）──
#
# Notice = {severity: info|warning|error, title, description?}


@pytest.mark.asyncio
async def test_notice_sent_when_client_declares(srv: ACPServer):
    srv._client_notices = True
    srv._send_notice("warning", "MCP server 未启用", "未加载 mcp 插件")
    (upd,) = _updates(srv)
    assert upd["sessionUpdate"] == "notice"
    assert upd["severity"] == "warning"
    assert upd["title"] == "MCP server 未启用"
    assert upd["description"] == "未加载 mcp 插件"


@pytest.mark.asyncio
async def test_notice_downgraded_to_message_without_capability(srv: ACPServer):
    """未声明 session.notices 时 MUST NOT 发 notice，但也**不能静默吞掉**。

    规范允许用普通输出代替；静默是本项目反复踩过的失败模式（用户永远不知道
    「MCP server 没起来」）。
    """
    srv._client_notices = False
    srv._send_notice("error", "MCP server 登记失败", "TimeoutError: 30s")
    ups = _updates(srv)
    assert all(u["sessionUpdate"] != "notice" for u in ups)
    text = "".join(u["content"]["text"] for u in ups if u["sessionUpdate"] == "agent_message_chunk")
    assert "MCP server 登记失败" in text and "TimeoutError" in text


def test_notices_capability_requires_object(srv: ACPServer):
    """session.notices 是 Option<结构体>：只有「值是对象」才算声明。"""
    srv._apply_client_capabilities({"session": {"notices": {}}})
    assert srv._client_notices is True
    for noise in (None, True, False, "yes", [], 1):
        srv._apply_client_capabilities({"session": {"notices": noise}})
        assert srv._client_notices is False, f"{noise!r} 不是结构体，不得当成已声明"
