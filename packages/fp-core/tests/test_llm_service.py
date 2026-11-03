"""测试 LLMService — 纯 LLM 调用层

覆盖重点：
- chat：正常调用、tools、tool_calls 转换、overrides 合并
- chat_stream：content/reasoning/tool_call 增量累积、usage、done
- chat_stream 向后兼容降级（chat 被覆写时）
- summarize
"""

from typing import Any

import pytest

from fp_core.core.llm_client import APIError, CompletionResponse, StreamChunk
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
    async def test_stream_skips_empty_deltas(self):
        """空 delta 不产事件：provider 在流首/工具轮会发 reasoning_content="" 空帧

        空帧若下传，前端会把它当成「思考块已存在 / 正文已到」：
        无思考的轮次凭空长出「思考 0 字」空块，且有正文时会把思考栏提前折叠
        （患者主诉：思考栏该展开的时候没展开）。
        """

        class FakeCompletions:
            async def create_stream(self, **kwargs):
                yield StreamChunk({"choices": [{"delta": {"reasoning_content": ""}}]})
                yield StreamChunk({"choices": [{"delta": {"content": ""}}]})
                yield StreamChunk({"choices": [{"delta": {"reasoning_content": "想"}}]})
                yield StreamChunk({"choices": [{"delta": {"content": ""}}]})
                yield StreamChunk({"choices": [{"delta": {"content": "答"}}]})

        client = _make_fake_client()
        client.chat.completions = FakeCompletions()

        service = self._make_service(client)
        events = [e async for e in service.chat_stream([])]

        assert [e.type for e in events] == ["reasoning", "content", "done"]
        assert [e.text for e in events[:-1]] == ["想", "答"]
        # 累积结果不受空帧影响
        assert events[-1].data == {"role": "assistant", "content": "答", "reasoning_content": "想"}

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


class TestReasoningContentPreserved:
    """思考模型的 reasoning_content 在 chat/chat_stream 结果中保留"""

    @pytest.mark.asyncio
    async def test_chat_keeps_reasoning(self):
        client = _make_fake_client()

        async def fake_create(**kwargs):
            data = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "answer",
                            "reasoning_content": "think-1",
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
            return CompletionResponse(data)

        client.chat.completions.create = fake_create
        svc = LLMService(client, LLMConfig(model="m"))
        result = await svc.chat([{"role": "user", "content": "hi"}])
        assert result.message["reasoning_content"] == "think-1"
        assert result.message["content"] == "answer"

    @pytest.mark.asyncio
    async def test_chat_stream_keeps_reasoning(self):
        client = _make_fake_client()

        async def fake_create_stream(**kwargs):
            chunks = [
                StreamChunk({"choices": [{"delta": {"reasoning_content": "r1"}}]}),
                StreamChunk({"choices": [{"delta": {"reasoning_content": "r2"}}]}),
                StreamChunk({"choices": [{"delta": {"content": "ans"}, "finish_reason": "stop"}]}),
            ]
            for c in chunks:
                yield c

        client.chat.completions.create_stream = fake_create_stream
        svc = LLMService(client, LLMConfig(model="m"))
        events = [e async for e in svc.chat_stream([{"role": "user", "content": "hi"}])]
        done = next(e for e in events if e.type == "done")
        assert done.data["reasoning_content"] == "r1r2"
        assert done.data["content"] == "ans"


class TestSummarizeNoThinking:
    """summarize 强制关闭思考（按激活 provider 的原生格式）"""

    @pytest.mark.asyncio
    async def test_summarize_overrides_extra_body(self):
        from fp_core import config as _cfg

        client = _make_fake_client()
        captured = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            data = {"choices": [{"message": {"role": "assistant", "content": "摘要"}, "finish_reason": "stop"}]}
            return CompletionResponse(data)

        client.chat.completions.create = fake_create
        svc = LLMService(client, LLMConfig(model="m", extra_body={"enable_thinking": True}))
        out = await svc.summarize("some text")
        assert out == "摘要"
        # 配置的 enable_thinking:True 被覆盖为关思考参数（具体格式随激活 provider）
        assert captured["extra_body"] == _cfg.no_thinking_body()
        assert captured["extra_body"]  # 非空


class TestRetry:
    """瞬时错误退避重试——无人值守时 API 抖动不应终结整轮"""

    def _ok_response(self, content="ok"):
        return CompletionResponse({
            "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]
        })

    def _service(self, create_impl, *, retry_count=3, on_retry=None):
        client = _make_fake_client()
        client.chat.completions.create = create_impl
        cfg = LLMConfig(model="m", retry_count=retry_count, retry_base_delay=0.0, retry_max_delay=0.0)
        return LLMService(client, cfg, on_retry=on_retry)

    @pytest.mark.asyncio
    async def test_retry_then_success(self):
        calls = {"n": 0}

        async def create(**kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise APIError("连接失败", status_code=0)
            return self._ok_response()

        svc = self._service(create)
        result = await svc.chat([{"role": "user", "content": "hi"}])
        assert result.message["content"] == "ok"
        assert calls["n"] == 3  # 初次 + 2 次重试后成功

    @pytest.mark.asyncio
    async def test_retry_exhausted_raises(self):
        calls = {"n": 0}

        async def create(**kwargs):
            calls["n"] += 1
            raise APIError("超时", status_code=0)

        svc = self._service(create, retry_count=2)
        with pytest.raises(APIError):
            await svc.chat([])
        assert calls["n"] == 3  # 初次 + 2 次重试

    @pytest.mark.asyncio
    async def test_no_retry_on_client_error(self):
        calls = {"n": 0}

        async def create(**kwargs):
            calls["n"] += 1
            raise APIError("bad request", status_code=400)

        svc = self._service(create)
        with pytest.raises(APIError):
            await svc.chat([])
        assert calls["n"] == 1  # 4xx 参数类错误不重试

    @pytest.mark.asyncio
    async def test_retry_disabled(self):
        calls = {"n": 0}

        async def create(**kwargs):
            calls["n"] += 1
            raise APIError("x", status_code=503)

        svc = self._service(create, retry_count=0)
        with pytest.raises(APIError):
            await svc.chat([])
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_on_retry_callback(self):
        seen = []
        calls = {"n": 0}

        async def create(**kwargs):
            calls["n"] += 1
            if calls["n"] < 2:
                raise APIError("boom", status_code=429)
            return self._ok_response()

        svc = self._service(create, on_retry=lambda a, m, e, d: seen.append((a, m, str(e))))
        await svc.chat([])
        assert seen and seen[0][0] == 1 and seen[0][1] == 3

    @pytest.mark.asyncio
    async def test_on_retry_callback_exception_swallowed(self):
        calls = {"n": 0}

        async def create(**kwargs):
            calls["n"] += 1
            if calls["n"] < 2:
                raise APIError("boom", status_code=500)
            return self._ok_response()

        def bad_cb(*a):
            raise RuntimeError("callback bug")

        svc = self._service(create, on_retry=bad_cb)
        result = await svc.chat([])
        assert result.message["content"] == "ok"

    @pytest.mark.asyncio
    async def test_stream_retry_before_first_token(self):
        attempts = {"n": 0}

        class FakeCompletions:
            async def create_stream(self, **kwargs):
                attempts["n"] += 1
                if attempts["n"] < 2:
                    raise APIError("connect", status_code=0)
                yield StreamChunk({"choices": [{"delta": {"content": "ok"}}]})

        client = _make_fake_client()
        client.chat.completions = FakeCompletions()
        cfg = LLMConfig(model="m", retry_count=3, retry_base_delay=0.0, retry_max_delay=0.0)
        svc = LLMService(client, cfg)
        events = [e async for e in svc.chat_stream([{"role": "user", "content": "hi"}])]
        assert [e.type for e in events] == ["content", "done"]
        assert attempts["n"] == 2

    @pytest.mark.asyncio
    async def test_stream_no_retry_after_content(self):
        attempts = {"n": 0}

        class FakeCompletions:
            async def create_stream(self, **kwargs):
                attempts["n"] += 1
                yield StreamChunk({"choices": [{"delta": {"content": "partial"}}]})
                raise APIError("mid-stream fail", status_code=0)

        client = _make_fake_client()
        client.chat.completions = FakeCompletions()
        cfg = LLMConfig(model="m", retry_count=3, retry_base_delay=0.0, retry_max_delay=0.0)
        svc = LLMService(client, cfg)
        got = []
        with pytest.raises(APIError):
            async for e in svc.chat_stream([]):
                got.append(e)
        assert got and got[0].type == "content"
        assert attempts["n"] == 1  # 已产出内容 → 不重试，避免重复展示


class TestIsRetryable:
    """is_retryable 的错误归类"""

    def test_retryable_statuses(self):
        from fp_core.core.llm_service import is_retryable

        assert is_retryable(APIError("x", status_code=0))  # 连接失败/超时
        assert is_retryable(APIError("x", status_code=429))
        assert is_retryable(APIError("x", status_code=500))
        assert is_retryable(APIError("x", status_code=503))

    def test_non_retryable(self):
        from fp_core.core.llm_service import is_retryable

        assert not is_retryable(APIError("x", status_code=400))
        assert not is_retryable(APIError("x", status_code=401))
        assert not is_retryable(APIError("x", status_code=422))
        assert not is_retryable(ValueError("boom"))


class TestRetryDelay429:
    """429 走更陡的退避阶梯（并发槽位型限流需撑过占用窗口）。

    抖动取 [0.5·d, d]，故各断言用区间而非定值，避免 flaky。
    base=1.0 / cap=30.0 下：
      429 → 4^attempt = 1 / 4 / 16  → 抖动后 [0.5,1] / [2,4] / [8,16]
      其它 → 2^attempt = 1 / 2 / 4  → 抖动后 [0.5,1] / [1,2] / [2,4]
    两组在 attempt>=1 后区间不重叠 → 断言安全。
    """

    def _svc(self, *, base=1.0, cap=30.0):
        cfg = LLMConfig(model="m", retry_base_delay=base, retry_max_delay=cap)
        return LLMService(client=None, config=cfg)

    def test_429_steeper_than_generic(self):
        svc = self._svc()
        for attempt in (1, 2):
            d429 = svc._retry_delay(attempt, APIError("x", status_code=429))
            d500 = svc._retry_delay(attempt, APIError("x", status_code=500))
            assert d429 > d500, f"attempt={attempt}: 429 应更陡 ({d429} vs {d500})"
        # attempt=2 时下界 8 仍高于通用路径上界 4
        assert svc._retry_delay(2, APIError("x", status_code=429)) >= 8.0
        assert svc._retry_delay(2, APIError("x", status_code=500)) <= 4.0

    def test_429_first_attempt_still_short(self):
        """首跳保持短间隔 —— 429 响应廉价，早探测几乎无成本。"""
        svc = self._svc()
        d0 = svc._retry_delay(0, APIError("x", status_code=429))
        assert 0.5 <= d0 <= 1.0

    def test_429_respects_cap(self):
        svc = self._svc(cap=2.0)
        for attempt in (0, 1, 2, 5):
            assert svc._retry_delay(attempt, APIError("x", status_code=429)) <= 2.0

    def test_generic_and_no_exc_unchanged(self):
        """非 429 与不传 exc 的旧调用方式保持原 2^attempt 行为。"""
        svc = self._svc()
        assert svc._retry_delay(2, APIError("x", status_code=500)) <= 4.0
        assert svc._retry_delay(2) <= 4.0
        assert svc._retry_delay(2, ValueError("boom")) <= 4.0

    def test_429_not_applied_to_connect_error(self):
        """status_code=0（连接失败/超时）不该误入 429 阶梯。"""
        svc = self._svc()
        assert svc._retry_delay(2, APIError("x", status_code=0)) <= 4.0
