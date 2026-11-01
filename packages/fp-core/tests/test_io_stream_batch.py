"""WebSocketIO 流式批处理契约 — 空 token 不得伪造事件

背景：provider 会在流首/无思考轮发 `reasoning_content=""`、在思考中途发
`content=""` 空帧。空帧一旦下发成 `thinking_content` / `chunk` 事件，前端会据此
建出「思考 0 字」空块，或在思考仍在进行时就把思考栏折叠（表现为「思考栏没展开」）。
"""

import asyncio

import pytest


class _FakeBus:
    def __init__(self):
        self.events: list[dict] = []

    async def publish(self, event: dict):
        self.events.append(event)


@pytest.mark.asyncio
async def test_empty_tokens_emit_no_events():
    from fp_core.core.io import WebSocketIO

    bus = _FakeBus()
    io = WebSocketIO(bus)
    io.stream_think("")
    io.stream_write("")
    io.stream_end()  # 冲刷残余（缓冲区里只有空串时应无事件）
    await asyncio.sleep(0)

    types = [e["type"] for e in bus.events]
    assert types == ["stream_end"], f"空帧不应产生 thinking_content/chunk：{types}"


@pytest.mark.asyncio
async def test_real_tokens_still_batched():
    from fp_core.core.io import WebSocketIO

    bus = _FakeBus()
    io = WebSocketIO(bus)
    io.stream_think("想")
    io.stream_think("考")
    io.stream_write("答")
    io.stream_end()  # stream_end 前冲刷，顺序安全
    await asyncio.sleep(0)

    think = [e for e in bus.events if e["type"] == "thinking_content"]
    chunk = [e for e in bus.events if e["type"] == "chunk"]
    assert len(think) == 1 and think[0]["token"] == "想考"  # 两 token 合并成一批
    assert len(chunk) == 1 and chunk[0]["content"] == "答"
    # 冲刷发生在 stream_end 之前
    assert [e["type"] for e in bus.events][-1] == "stream_end"
