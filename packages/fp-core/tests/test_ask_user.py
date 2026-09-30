"""ask_user 测试 — 权威注入原则 + 单 flight + 降级路径 + 结构化契约 v2

契约：ASYNC_AGENT_DESIGN.md §2/§7
- 缺 io → unavailable 降级（不挂死，I1）
- 有 io → 回答以【用户回答 …】入 pending 队列（user 角色，环顶注入 — I2）
  + tool result 只回执 reply_file（收据，不含人类话语）
- 单 flight：并发第二个 → busy
- v2：options/suggest/ask_id 结构化透传；timeout 有界；deferred 不注入
- WebSocketIO：事件 schema v2 + feed_reply 幂等对账 + 快照
"""

import asyncio
import json
import os

import pytest

from fp_core.tools.extensions import background_plugin as bp


class _FakeIO:
    """最小 IO 通道 stub：ask 立即返回预设回答（结构化契约 v2）"""

    def __init__(self, reply: str):
        self._reply = reply
        self.asked: list[str] = []
        self.ask_kwargs: dict = {}

    async def ask(
        self,
        prompt: str,
        *,
        options: list[str] | None = None,
        suggest: str = "",
        ask_id: str | None = None,
    ) -> str:
        self.asked.append(prompt)
        self.ask_kwargs = {"options": options, "suggest": suggest, "ask_id": ask_id}
        await asyncio.sleep(0)
        return self._reply


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """每个用例：清空 pending 队列与 ask 锁（模块级状态隔离）"""
    bp.pending_inject.clear()
    # 锁可能被上个用例遗留的等待者占用 → 重置
    if bp.ask_lock.locked():
        monkeypatch.setattr(bp, "ask_lock", asyncio.Lock())
    yield
    bp.pending_inject.clear()


@pytest.mark.asyncio
async def test_ask_missing_prompt():
    res = json.loads(await bp._ask_user({}))
    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_ask_no_io_degrades(monkeypatch):
    """缺 io（headless/worker）→ unavailable，不挂死（I1）"""
    import fp_core.core.agent as agent_mod

    monkeypatch.setattr(agent_mod, "get_current_io", lambda: None)
    res = json.loads(await bp._ask_user({"prompt": "在吗？"}))
    assert res["status"] == "unavailable"
    assert res["error"] == "no_io"


@pytest.mark.asyncio
async def test_ask_answered_injects_user_msg_not_in_tool_result(monkeypatch):
    """核心不变量 I2：人类话语只以 user 角色到达；tool result 只有收据"""
    import fp_core.core.agent as agent_mod

    fake = _FakeIO("选 A")
    monkeypatch.setattr(agent_mod, "get_current_io", lambda: fake)

    raw = await bp._ask_user({"prompt": "选哪个？", "suggest": "A"})
    res = json.loads(raw)

    assert res["status"] == "answered"
    # 收据含 reply_file，但不含人类话语
    assert "选 A" not in raw
    assert os.path.exists(res["reply_file"])

    # 回答已入 pending 队列（环顶 drain 前不在对话里）；in_reply_to 随消息携带
    drained = bp.drain_ready()
    assert len(drained) == 1
    assert drained[0]["kind"] == "user_reply"
    assert drained[0]["content"].startswith("【用户回答 ")
    assert res["ask_id"] in drained[0]["content"]
    assert "选 A" in drained[0]["content"]
    # drain 恰好一次
    assert bp.drain_ready() == []

    # 结构化契约：suggest/options/ask_id 均传进 io.ask
    assert fake.ask_kwargs["suggest"] == "A"
    assert fake.ask_kwargs["ask_id"] == res["ask_id"]


@pytest.mark.asyncio
async def test_ask_single_flight_busy(monkeypatch):
    """单 flight：第一个 ask 挂起期间，第二个 → busy"""
    import fp_core.core.agent as agent_mod

    started = asyncio.Event()

    class _SlowIO:
        async def ask(
            self,
            prompt: str,
            *,
            options: list[str] | None = None,
            suggest: str = "",
            ask_id: str | None = None,
        ) -> str:
            started.set()
            await asyncio.sleep(30)
            return "too late"

    monkeypatch.setattr(agent_mod, "get_current_io", lambda: _SlowIO())

    task = asyncio.create_task(bp._ask_user({"prompt": "第一个"}))
    await started.wait()
    second = json.loads(await bp._ask_user({"prompt": "第二个"}))
    assert second["status"] == "busy"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_drain_empty():
    assert bp.drain_ready() == []


# ── 结构化契约 v2 新特性 ────────────────────────────────


@pytest.mark.asyncio
async def test_ask_timeout_bounded(monkeypatch):
    """timeout 有界等待 → status=timeout，不挂死（copilot P0 项）"""
    import fp_core.core.agent as agent_mod

    class _HangIO:
        async def ask(self, prompt, *, options=None, suggest="", ask_id=None) -> str:
            await asyncio.sleep(30)
            return "late"

    monkeypatch.setattr(agent_mod, "get_current_io", lambda: _HangIO())
    res = json.loads(await bp._ask_user({"prompt": "在吗？", "timeout": 0.05}))
    assert res["status"] == "timeout"
    assert res["ask_id"].startswith("ask_")
    # 超时后无注入
    assert bp.drain_ready() == []


@pytest.mark.asyncio
async def test_ask_deferred_no_injection(monkeypatch):
    """deferred（ACP）：空答 + ask_deferred → status=deferred，不注入不回执"""
    import fp_core.core.agent as agent_mod

    class _ACPishIO:
        ask_deferred = True

        async def ask(self, prompt, *, options=None, suggest="", ask_id=None) -> str:
            return ""  # 问题已展示，回答走下一轮

    monkeypatch.setattr(agent_mod, "get_current_io", lambda: _ACPishIO())
    res = json.loads(await bp._ask_user({"prompt": "选哪个？"}))
    assert res["status"] == "deferred"
    assert "下一条消息" in res["note"]
    assert bp.drain_ready() == []
    assert "reply_file" not in res


@pytest.mark.asyncio
async def test_ask_options_and_timeout_passed_through(monkeypatch):
    """options/timeout/suggest 全量透传 io.ask"""
    import fp_core.core.agent as agent_mod

    fake = _FakeIO("y")
    monkeypatch.setattr(agent_mod, "get_current_io", lambda: fake)

    res = json.loads(await bp._ask_user({"prompt": "执行吗？", "options": ["y", "n"], "suggest": "n", "timeout": 60}))
    assert res["status"] == "answered"
    assert fake.ask_kwargs["options"] == ["y", "n"]
    assert fake.ask_kwargs["suggest"] == "n"


@pytest.mark.asyncio
async def test_empty_reply_empty_status(monkeypatch):
    """非 deferred 通道的空答 → empty（不自动采纳推荐值，不伪造回答）"""
    import fp_core.core.agent as agent_mod

    monkeypatch.setattr(agent_mod, "get_current_io", lambda: _FakeIO(""))
    res = json.loads(await bp._ask_user({"prompt": "在吗？"}))
    assert res["status"] == "empty"
    assert bp.drain_ready() == []


# ── WebSocketIO 结构化契约（事件 v2 + feed_reply 幂等） ──


class _FakeBus:
    def __init__(self):
        self.events: list[dict] = []

    async def publish(self, event: dict):
        self.events.append(event)


@pytest.mark.asyncio
async def test_websocket_io_ask_structured_event_and_idempotent_reply():
    from fp_core.core.io import WebSocketIO

    io = WebSocketIO(_FakeBus())
    task = asyncio.create_task(io.ask("执行吗？", options=["y", "n"], suggest="n", ask_id="ask_fixed1"))
    await asyncio.sleep(0)

    # 事件 schema v2：version/ask_id/options/suggest 齐备
    ev = io._event_bus.events[-1]
    assert ev["type"] == "ask"
    assert ev["version"] == 2
    assert ev["ask_id"] == "ask_fixed1"
    assert ev["options"] == ["y", "n"]
    assert ev["suggest"] == "n"

    # 快照携带完整元数据（跨连接恢复）
    snap = io.pending_ask_snapshot()
    assert snap is not None and snap["ask_id"] == "ask_fixed1"

    # 精确对账 + 幂等：答对 id 成功；重复答 → False
    assert io.feed_reply("y", ask_id="ask_fixed1") is True
    assert await task == "y"
    assert io.feed_reply("y", ask_id="ask_fixed1") is False
    # 无等待者 → False（调用方按普通消息处理）
    assert io.feed_reply("z") is False
    # 答复后快照清空
    assert io.pending_ask_snapshot() is None


@pytest.mark.asyncio
async def test_websocket_io_feed_reply_latest_without_id():
    from fp_core.core.io import WebSocketIO

    io = WebSocketIO(_FakeBus())
    t1 = asyncio.create_task(io.ask("问题一"))
    t2 = asyncio.create_task(io.ask("问题二"))
    await asyncio.sleep(0)
    first_id = io._event_bus.events[0]["ask_id"]
    # 无 ask_id → 唤醒最新
    assert io.feed_reply("答二") is True
    assert await t2 == "答二"
    assert io.feed_reply("答一", ask_id=first_id) is True
    assert await t1 == "答一"
