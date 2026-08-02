"""测试 LLMService — 纯 LLM 调用层

覆盖重点：
- chat：正常调用、tools、tool_calls 转换、overrides 合并
- chat_stream：content/reasoning/tool_call 增量累积、usage、done
- chat_stream 向后兼容降级（chat 被覆写时）
- summarize
"""

from typing import Any

import pytest

from fp_core.core.llm_client import CompletionResponse, StreamChunk
from fp_core.core.llm_service import LLMConfig, LLMResult, LLMService


# 动态构造的假 LLM client。返回 Any：这些对象只是 mock 容器，
# 类型检查器无法推断其属性，标注 Any 避免对每个测试里的
# client.chat.completions.create = ... 误报。
def _make_fake_client() -> Any:
    client: Any = type("Client", (), {})()
    chat: Any = type("Chat", (), {})()
    completions: Any = type("Completions", (), {})()
    client.chat = chat
    chat.completions = completions
    return client


class TestLLMConfig:
    def test_defaults(self):
        c = LLMConfig()
        assert c.model == ""
        assert c.temperature == 0.7
        assert c.max_tokens == 4096
        assert c.extra_body == {"enable_thinking": False}


class TestChat:
    def _make_service(self, client=None, config=None):
        client = client or object()
        return LLMService(client, config or LLMConfig(model="gpt-4o"))

    def _fake_response(self, content="hi", tool_calls=None, usage=None):
        data = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ]
        }
        if tool_calls is not None:
            data["choices"][0]["message"]["tool_calls"] = tool_calls
        if usage is not None:
            data["usage"] = usage
        return CompletionResponse(data)

    @pytest.mark.asyncio
    async def test_chat_basic(self):
        class FakeCompletions:
            def __init__(self):
                self.create: Any = None

        client = _make_fake_client()
        client.chat.completions = FakeCompletions()

        async def fake_create(**kwargs):
            assert kwargs["model"] == "gpt-4o"
            assert kwargs["temperature"] == 0.7
            assert kwargs["max_tokens"] == 4096
            return self._fake_response(content="你好")

        client.chat.completions.create = fake_create

        service = self._make_service(client)
        result = await service.chat([{"role": "user", "content": "hi"}])

        assert isinstance(result, LLMResult)
        assert result.message["role"] == "assistant"
        assert result.message["content"] == "你好"
        assert result.usage is None

    @pytest.mark.asyncio
    async def test_chat_with_tools_and_usage(self):
        client = _make_fake_client()

        async def fake_create(**kwargs):
            assert "tools" in kwargs
            return self._fake_response(usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})

        client.chat.completions.create = fake_create

        service = self._make_service(client)
        result = await service.chat(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function"}],
        )
        assert result.usage is not None
        assert result.usage["total_tokens"] == 3

    @pytest.mark.asyncio
    async def test_chat_translates_tool_calls(self):
        client = _make_fake_client()

        async def fake_create(**kwargs):
            return self._fake_response(
                content="",
                tool_calls=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"cmd":"ls"}'},
                    }
                ],
            )

        client.chat.completions.create = fake_create

        service = self._make_service(client)
        result = await service.chat([{"role": "user", "content": "hi"}])

        tc = result.message["tool_calls"][0]
        assert tc["id"] == "c1"
        assert tc["function"]["name"] == "bash"
        assert tc["function"]["arguments"] == '{"cmd":"ls"}'

    @pytest.mark.asyncio
    async def test_chat_overrides_config(self):
        client = _make_fake_client()

        captured = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return self._fake_response()

        client.chat.completions.create = fake_create

        service = self._make_service(client)
        await service.chat([], model="o3", temperature=0.1, max_tokens=100, extra_body={"x": 1})

        assert captured["model"] == "o3"
        assert captured["temperature"] == 0.1
        assert captured["max_tokens"] == 100
        assert captured["extra_body"] == {"x": 1}

    @pytest.mark.asyncio
    async def test_model_property(self):
        service = self._make_service()
        assert service.model == "gpt-4o"

    @pytest.mark.asyncio
    async def test_summarize(self):
        client = _make_fake_client()

        captured = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return self._fake_response(content="摘要结果")

        client.chat.completions.create = fake_create

        service = self._make_service(client)
        summary = await service.summarize("很长的文本")

        assert summary == "摘要结果"
        # system prompt + user instruction
        assert captured["messages"][0]["role"] == "system"
        assert "压缩" in captured["messages"][1]["content"]
        assert captured["max_tokens"] == 1000


class TestChatStream:
    def _make_service(self, client=None):
        client = client or object()
        return LLMService(client, LLMConfig(model="gpt-4o"))

    @pytest.mark.asyncio
    async def test_stream_content_and_done(self):
        class FakeCompletions:
            async def create_stream(self, **kwargs):
                yield StreamChunk({"choices": [{"delta": {"content": "你"}}]})
                yield StreamChunk({"choices": [{"delta": {"content": "好"}}]})
                yield StreamChunk({"choices": [{"delta": {}}], "usage": {"total_tokens": 5}})
                yield StreamChunk({"choices": [{"delta": {}}]})

        client = _make_fake_client()
        client.chat.completions = FakeCompletions()

        service = self._make_service(client)
        events = [e async for e in service.chat_stream([{"role": "user", "content": "hi"}])]

        types = [e.type for e in events]
        # usage 恰好一次：chunk 中出现即 yield，流末不再补发（曾因 53c066c 叠加补发而重复两次）
        assert types == ["content", "content", "usage", "done"]

        # done 携带完整消息
        done = events[-1]
        assert done.data == {"role": "assistant", "content": "你好"}

        # usage 事件：恰好一次，不重复
        usage_events = [e for e in events if e.type == "usage"]
        assert len(usage_events) == 1
        assert usage_events[0].data is not None
        assert usage_events[0].data["total_tokens"] == 5

    @pytest.mark.asyncio
    async def test_stream_reasoning_events(self):
        class FakeCompletions:
            async def create_stream(self, **kwargs):
                yield StreamChunk({"choices": [{"delta": {"reasoning_content": "思考中"}}]})
                yield StreamChunk({"choices": [{"delta": {"content": "回答"}}]})

        client = _make_fake_client()
        client.chat.completions = FakeCompletions()

        service = self._make_service(client)
        events = [e async for e in service.chat_stream([])]

        assert [e.type for e in events] == ["reasoning", "content", "done"]
        assert events[0].text == "思考中"

    @pytest.mark.asyncio
    async def test_stream_tool_call_delta_accumulation(self):
        """工具调用增量分片正确拼装"""

        class FakeCompletions:
            async def create_stream(self, **kwargs):
                yield StreamChunk({
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "c1",
                                        "type": "function",
                                        "function": {"name": "bash", "arguments": '{"cmd":'},
                                    },
                                ]
                            }
                        }
                    ]
                })
                yield StreamChunk({
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"arguments": '"ls"}'}},
                                ]
                            }
                        }
                    ]
                })

        client = _make_fake_client()
        client.chat.completions = FakeCompletions()

        service = self._make_service(client)
        events = [e async for e in service.chat_stream([])]

        done = events[-1]
        assert done.type == "done"
        assert done.data is not None
        tc = done.data["tool_calls"][0]
        assert tc["id"] == "c1"
        assert tc["function"]["name"] == "bash"
        assert tc["function"]["arguments"] == '{"cmd":"ls"}'

    @pytest.mark.asyncio
    async def test_stream_fallback_when_chat_overridden(self):
        """chat 被覆写时降级为非流式，yield 单个 done"""
        client = _make_fake_client()
        service = self._make_service(client)

        async def fake_chat(messages, tools=None, **overrides):
            return LLMResult(message={"role": "assistant", "content": "降级结果"})

        service.chat = fake_chat  # 覆写 chat（模拟测试 mock）
        events = [e async for e in service.chat_stream([])]

        assert len(events) == 1
        assert events[0].type == "done"
        assert events[0].data is not None
        assert events[0].data["content"] == "降级结果"
