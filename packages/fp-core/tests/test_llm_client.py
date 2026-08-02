"""测试 llm_client — 自实现 OpenAI HTTP 客户端

覆盖重点：
- 数据类解析：Message(<think> 提取)、ToolCall、CompletionResponse、StreamChunk
- Completions.create：正常/连接错误/超时/HTTP错误/JSON错误/请求体构建
- Completions.create_stream：SSE 解析、错误分支
- Client._headers / close
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from fp_core.core.llm_client import (
    APIError,
    Client,
    CompletionResponse,
    Completions,
    Message,
    MessageChoice,
    StreamChunk,
    ToolCall,
    ToolCallFunction,
)

# ═══════════════════════════════════════════════════════════
# 数据类解析
# ═══════════════════════════════════════════════════════════


class TestModelClasses:
    def test_tool_call_function(self):
        f = ToolCallFunction({"name": "bash", "arguments": '{"cmd":"ls"}'})
        assert f.name == "bash"
        assert f.arguments == '{"cmd":"ls"}'

    def test_tool_call(self):
        tc = ToolCall({"id": "call_1", "type": "function", "function": {"name": "ls"}})
        assert tc.id == "call_1"
        assert tc.type == "function"
        assert tc.function.name == "ls"

    def test_tool_call_defaults(self):
        tc = ToolCall({})
        assert tc.id == ""
        assert tc.type == "function"
        assert tc.function.name == ""

    def test_message_basic(self):
        m = Message({"role": "assistant", "content": "你好"})
        assert m.role == "assistant"
        assert m.content == "你好"
        assert m.tool_calls is None

    def test_message_with_tool_calls(self):
        m = Message({"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "function": {"name": "bash"}}]})
        assert m.tool_calls is not None
        assert len(m.tool_calls) == 1
        assert m.tool_calls[0].function.name == "bash"

    def test_message_extracts_think_tags(self):
        """content 以 <think> 开头时提取 reasoning_content"""
        m = Message({"role": "assistant", "content": "<think>内部思考</think>最终回答"})
        assert m.reasoning_content == "内部思考"
        assert m.content == "最终回答"

    def test_message_think_only_no_after(self):
        m = Message({"role": "assistant", "content": "<think>只有思考</think>"})
        assert m.reasoning_content == "只有思考"
        assert m.content is None

    def test_message_keeps_think_when_incomplete(self):
        """<think> 未闭合时不做提取"""
        m = Message({"role": "assistant", "content": "<think>未闭合"})
        assert m.reasoning_content is None
        assert m.content == "<think>未闭合"

    def test_message_prefers_native_reasoning_content(self):
        """原生 reasoning_content 存在时不做 <think> 提取"""
        m = Message({"role": "assistant", "content": "<think>x</think>y", "reasoning_content": "原生"})
        assert m.reasoning_content == "原生"
        assert m.content == "<think>x</think>y"

    def test_message_choice(self):
        c = MessageChoice({"index": 0, "message": {"content": "hi"}, "finish_reason": "stop"})
        assert c.index == 0
        assert c.message.content == "hi"
        assert c.finish_reason == "stop"

    def test_completion_response(self):
        data = {
            "id": "cmpl-1",
            "object": "chat.completion",
            "created": 123,
            "model": "gpt",
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        r = CompletionResponse(data)
        assert r.id == "cmpl-1"
        assert r.model == "gpt"
        assert len(r.choices) == 1
        assert r.usage is not None
        assert r.usage["total_tokens"] == 2

    def test_stream_chunk_content(self):
        c = StreamChunk({"choices": [{"delta": {"content": "你"}}]})
        assert c.content == "你"
        assert c.finish_reason is None

    def test_stream_chunk_reasoning_and_tools(self):
        c = StreamChunk({
            "choices": [
                {
                    "delta": {"reasoning_content": "思考", "tool_calls": [{"id": "c1"}]},
                    "finish_reason": "tool_calls",
                }
            ]
        })
        assert c.reasoning_content == "思考"
        assert c.tool_calls == [{"id": "c1"}]
        assert c.finish_reason == "tool_calls"

    def test_stream_chunk_usage(self):
        c = StreamChunk({"choices": [{"delta": {}}], "usage": {"total_tokens": 5}})
        assert c.usage == {"total_tokens": 5}


# ═══════════════════════════════════════════════════════════
# Client 基础
# ═══════════════════════════════════════════════════════════


class TestClient:
    def test_base_url_strips_trailing_slash(self):
        c = Client(api_key="k", base_url="https://api.example.com/v1/")
        assert c.base_url == "https://api.example.com/v1"

    def test_headers(self):
        c = Client(api_key="sk-123", base_url="https://api.example.com/v1")
        h = c._headers()
        assert h["Authorization"] == "Bearer sk-123"
        assert h["Content-Type"] == "application/json"
        assert h["Accept"] == "application/json"

    @pytest.mark.asyncio
    async def test_close(self):
        c = Client(api_key="k")
        c._session.aclose = AsyncMock()
        await c.close()
        c._session.aclose.assert_awaited_once()


# ═══════════════════════════════════════════════════════════
# Completions.create（非流式）
# ═══════════════════════════════════════════════════════════


class TestCompletionsCreate:
    def _make_client(self) -> Any:
        """_session 被替换为 mock；返回 Any 避免类型检查器按 httpx.AsyncClient 误报"""
        client = Client(api_key="k", base_url="https://api.example.com/v1")
        client._session = MagicMock()
        return client

    @pytest.mark.asyncio
    async def test_create_success(self):
        client = self._make_client()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "id": "cmpl-1",
            "model": "gpt",
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        }
        client._session.post = AsyncMock(return_value=resp)

        comp = Completions(client)
        result = await comp.create(model="gpt", messages=[{"role": "user", "content": "hi"}])

        assert isinstance(result, CompletionResponse)
        assert result.choices[0].message.content == "hi"
        # 请求 URL 与 body
        client._session.post.assert_awaited_once()
        assert client._session.post.await_args is not None
        args, kwargs = client._session.post.await_args
        assert args[0] == "https://api.example.com/v1/chat/completions"
        assert kwargs["json"]["model"] == "gpt"
        assert kwargs["json"]["messages"] == [{"role": "user", "content": "hi"}]
        assert "temperature" not in kwargs["json"]  # 未传则不加入

    @pytest.mark.asyncio
    async def test_create_with_all_params(self):
        client = self._make_client()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"choices": [{"message": {"content": "x"}}]}
        client._session.post = AsyncMock(return_value=resp)

        comp = Completions(client)
        await comp.create(
            model="gpt",
            messages=[],
            temperature=0.5,
            max_tokens=100,
            tools=[{"type": "function"}],
            extra_body={"enable_thinking": False},
        )
        assert client._session.post.await_args is not None
        _, kwargs = client._session.post.await_args
        body = kwargs["json"]
        assert body["temperature"] == 0.5
        assert body["max_tokens"] == 100
        assert body["tools"] == [{"type": "function"}]
        assert body["enable_thinking"] is False

    @pytest.mark.asyncio
    async def test_create_connect_error(self):
        client = self._make_client()
        client._session.post = AsyncMock(side_effect=httpx.ConnectError("boom"))
        comp = Completions(client)
        with pytest.raises(APIError) as ei:
            await comp.create(model="gpt", messages=[])
        assert ei.value.status_code == 0
        assert "连接失败" in str(ei.value)

    @pytest.mark.asyncio
    async def test_create_timeout_error(self):
        client = self._make_client()
        client._session.post = AsyncMock(side_effect=httpx.TimeoutException("slow"))
        comp = Completions(client)
        with pytest.raises(APIError) as ei:
            await comp.create(model="gpt", messages=[])
        assert ei.value.status_code == 0
        assert "超时" in str(ei.value)

    @pytest.mark.asyncio
    async def test_create_http_error(self):
        client = self._make_client()
        resp = MagicMock(status_code=429, text="rate limited")
        client._session.post = AsyncMock(return_value=resp)
        comp = Completions(client)
        with pytest.raises(APIError) as ei:
            await comp.create(model="gpt", messages=[])
        assert ei.value.status_code == 429
        assert "rate limited" in ei.value.body

    @pytest.mark.asyncio
    async def test_create_json_decode_error(self):
        client = self._make_client()
        resp = MagicMock(status_code=200)
        resp.json.side_effect = json.JSONDecodeError("bad", "", 0)
        client._session.post = AsyncMock(return_value=resp)
        comp = Completions(client)
        with pytest.raises(APIError) as ei:
            await comp.create(model="gpt", messages=[])
        assert "JSON 解析失败" in str(ei.value)


# ═══════════════════════════════════════════════════════════
# Completions.create_stream（流式）
# ═══════════════════════════════════════════════════════════


class _FakeStreamResp:
    """模拟 httpx stream 响应：按字节块喂 SSE 数据"""

    def __init__(self, chunks: list[str], status_code: int = 200, enter_error: Exception | None = None):
        self._chunks = chunks
        self.status_code = status_code
        self._enter_error = enter_error

    async def __aenter__(self):
        if self._enter_error is not None:
            raise self._enter_error
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self):
        return b""

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c.encode()


class TestCompletionsCreateStream:
    def _make_client(self) -> Any:
        """_session 被替换为 mock；返回 Any 避免类型检查器按 httpx.AsyncClient 误报"""
        client = Client(api_key="k", base_url="https://api.example.com/v1")
        client._session = MagicMock()
        return client

    @pytest.mark.asyncio
    async def test_stream_success_yields_chunks(self):
        client = self._make_client()
        sse = [
            'data: {"choices":[{"delta":{"content":"你"}}]}\n\n',
            'data: {"choices":[{"delta":{"content":"好"}}]}\n\n',
            "data: [DONE]\n\n",
        ]
        client._session.stream.return_value = _FakeStreamResp(sse)

        comp = Completions(client)
        chunks = [c async for c in comp.create_stream(model="gpt", messages=[])]

        assert len(chunks) == 2
        assert chunks[0].content == "你"
        assert chunks[1].content == "好"

    @pytest.mark.asyncio
    async def test_stream_splits_partial_lines(self):
        """SSE 字节分块跨越行边界也能正确解析"""
        client = self._make_client()
        raw = 'data: {"choices":[{"delta":{"content":"你"}}]}\n\n'
        # 拆成两段字节
        mid = len(raw) // 2
        client._session.stream.return_value = _FakeStreamResp([raw[:mid], raw[mid:]])

        comp = Completions(client)
        chunks = [c async for c in comp.create_stream(model="gpt", messages=[])]
        assert len(chunks) == 1
        assert chunks[0].content == "你"

    @pytest.mark.asyncio
    async def test_stream_skips_bad_json(self):
        """坏 JSON 行被跳过，不中断流"""
        client = self._make_client()
        sse = [
            "data: not-json\n\n",
            'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
            "data: [DONE]\n\n",
        ]
        client._session.stream.return_value = _FakeStreamResp(sse)
        comp = Completions(client)
        chunks = [c async for c in comp.create_stream(model="gpt", messages=[])]
        assert len(chunks) == 1
        assert chunks[0].content == "ok"

    @pytest.mark.asyncio
    async def test_stream_http_error(self):
        client = self._make_client()
        client._session.stream.return_value = _FakeStreamResp([], status_code=500)
        comp = Completions(client)
        with pytest.raises(APIError) as ei:
            async for _ in comp.create_stream(model="gpt", messages=[]):
                pass
        assert ei.value.status_code == 500

    @pytest.mark.asyncio
    async def test_stream_connect_error(self):
        client = self._make_client()
        client._session.stream.return_value = _FakeStreamResp([], enter_error=httpx.ConnectError("boom"))
        comp = Completions(client)
        with pytest.raises(APIError) as ei:
            async for _ in comp.create_stream(model="gpt", messages=[]):
                pass
        assert ei.value.status_code == 0

    @pytest.mark.asyncio
    async def test_stream_sets_headers_and_body(self):
        client = self._make_client()
        client._session.stream.return_value = _FakeStreamResp(["data: [DONE]\n\n"])
        comp = Completions(client)
        async for _ in comp.create_stream(model="gpt", messages=[], temperature=0.2):
            pass

        client._session.stream.assert_called_once()
        args, kwargs = client._session.stream.call_args
        assert args[0] == "POST"
        assert kwargs["headers"]["Accept"] == "text/event-stream"
        assert kwargs["json"]["stream"] is True
        assert kwargs["json"]["temperature"] == 0.2
