"""ask_user 测试 — 权威注入原则 + 单 flight + 降级路径

契约：ASYNC_AGENT_DESIGN.md §2/§7
- 缺 io → unavailable 降级（不挂死，I1）
- 有 io → 回答以【用户回答】入 pending 队列（user 角色，环顶注入 — I2）
  + tool result 只回执 reply_file（收据，不含人类话语）
- 单 flight：并发第二个 → busy
- suggest → 随 prompt 传递
"""

import asyncio
import json
import os

import pytest

from fp_core.tools.extensions import background_plugin as bp


class _FakeIO:
    """最小 IO 通道 stub：ask 立即返回预设回答"""

    def __init__(self, reply: str):
        self._reply = reply
        self.asked: list[str] = []

    async def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
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

    # 回答已入 pending 队列（环顶 drain 前不在对话里）
    drained = bp.drain_ready()
    assert len(drained) == 1
    assert drained[0]["kind"] == "user_reply"
    assert drained[0]["content"].startswith("【用户回答】")
    assert "选 A" in drained[0]["content"]
    # drain 恰好一次
    assert bp.drain_ready() == []

    # suggest 传进了 prompt
    assert "A" in fake.asked[0]


@pytest.mark.asyncio
async def test_ask_single_flight_busy(monkeypatch):
    """单 flight：第一个 ask 挂起期间，第二个 → busy"""
    import fp_core.core.agent as agent_mod

    started = asyncio.Event()

    class _SlowIO:
        async def ask(self, prompt: str) -> str:
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
