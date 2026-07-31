"""
LLMService — 纯 LLM 调用层

职责：
- 封装 LLM client 调用
- 输入：messages + tools → 输出：LLMResult(message, usage)
- 不处理 IO、不显示 spinner、不格式化输出
- 只做"消息→LLM→响应"的纯转换
"""

import types
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMConfig:
    """LLM 配置"""

    model: str = ""
    temperature: float = 0.7
    max_tokens: int = 4096
    extra_body: dict = field(default_factory=lambda: {"enable_thinking": False})


@dataclass
class LLMResult:
    """LLM 调用结果（message + usage 一起返回）"""

    message: dict[str, Any]  # {"role", "content", "tool_calls"?}
    usage: dict[str, Any] | None = None  # {"prompt_tokens", "completion_tokens", "total_tokens"}


@dataclass
class StreamEvent:
    """流式事件 — 逐个 yield 给调用方

    type 取值:
      - "content":    LLM 回复文本 token（event.text 为 token）
      - "reasoning":  思考过程 token（event.text 为 token）
      - "tool_call":  工具调用信息（event.data 为 tool_call dict）
      - "usage":      用量（event.data 为 usage dict）
      - "done":       流结束（event.data 为完整 assistant_msg dict）
    """

    type: str = ""
    text: str = ""
    data: dict | None = None


class LLMService:
    """LLM 调用服务"""

    def __init__(self, client, config: LLMConfig):
        """
        Args:
            client: LLM client 实例（core.llm_client.Client）
            config: LLM 配置
        """
        self._client = client
        self._config = config

    @property
    def model(self) -> str:
        return self._config.model

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **overrides,
    ) -> LLMResult:
        """
        调用 LLM 并返回 LLMResult(message, usage)。

        Args:
            messages: 消息列表
            tools: 工具定义列表（可选）
            **overrides: 覆盖 LLMConfig 中的字段（model, temperature, max_tokens 等）

        Returns:
            LLMResult(message, usage)

            message dict:
                {"role": "assistant", "content": "...", "tool_calls": [...]}
            usage dict:
                {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}
        """
        model = overrides.get("model", self._config.model)
        temperature = overrides.get("temperature", self._config.temperature)
        max_tokens = overrides.get("max_tokens", self._config.max_tokens)
        extra_body = overrides.get("extra_body", self._config.extra_body)

        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
        if extra_body:
            kwargs["extra_body"] = extra_body

        response = await self._client.chat.completions.create(**kwargs)
        message = response.choices[0].message
        usage = response.usage  # 可能为 None，由调用方处理

        msg: dict = {"role": "assistant", "content": message.content or ""}
        if message.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in message.tool_calls
            ]

        return LLMResult(message=msg, usage=usage)

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **overrides,
    ):
        """
        流式调用 LLM，逐个 yield StreamEvent。

        事件序列（典型）:
          1. reasoning_token → reasoning_token → ...  （思考过程，可选）
          2. content_token → content_token → ...      （回复文本）
          3. tool_call → ...                           （工具调用，可选）
          4. usage                                     （用量）
          5. done                                      （完整消息）

        调用方在 done 事件中获取完整的 assistant_msg。

        向后兼容：若 chat() 被覆写（如测试 mock），自动降级为
        非流式调用并 yield 单个 done 事件。
        """
        # ── 向后兼容：检测 chat() 是否被覆写（如测试 mock） ──
        bound_chat = self.chat
        if not isinstance(bound_chat, types.MethodType) or bound_chat.__func__ is not LLMService.chat:
            result = await self.chat(messages, tools=tools, **overrides)
            yield StreamEvent(type="done", data=result.message)
            return
        model = overrides.get("model", self._config.model)
        temperature = overrides.get("temperature", self._config.temperature)
        max_tokens = overrides.get("max_tokens", self._config.max_tokens)
        extra_body = overrides.get("extra_body", self._config.extra_body)

        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
        if extra_body:
            kwargs["extra_body"] = extra_body

        # 累积完整消息（用于最终构造 assistant_msg）
        content_chunks: list[str] = []
        reasoning_chunks: list[str] = []
        tool_call_acc: dict[int, dict] = {}  # index → {id, type, function: {name, arguments}}
        final_usage: dict | None = None

        async for chunk in self._client.chat.completions.create_stream(**kwargs):
            # ── 文本 token ──
            if chunk.content is not None:
                content_chunks.append(chunk.content)
                yield StreamEvent(type="content", text=chunk.content)

            # ── 思考 token ──
            if chunk.reasoning_content is not None:
                reasoning_chunks.append(chunk.reasoning_content)
                yield StreamEvent(type="reasoning", text=chunk.reasoning_content)

            # ── 工具调用 delta ──
            if chunk.tool_calls:
                for tc in chunk.tool_calls:
                    idx = tc.get("index", 0)
                    if idx not in tool_call_acc:
                        tool_call_acc[idx] = {
                            "id": tc.get("id", ""),
                            "type": tc.get("type", "function"),
                            "function": {"name": "", "arguments": ""},
                        }
                    acc = tool_call_acc[idx]
                    if tc.get("id"):
                        acc["id"] = tc["id"]
                    if tc.get("function", {}).get("name"):
                        acc["function"]["name"] += tc["function"]["name"]
                    if tc.get("function", {}).get("arguments"):
                        acc["function"]["arguments"] += tc["function"]["arguments"]

            # ── usage（可能在任意 chunk 中出现，通常在最后一个或倒数第二个） ──
            if chunk.usage:
                final_usage = chunk.usage
                yield StreamEvent(type="usage", data=final_usage)

        # ── 构造完整 assistant_msg ──
        content = "".join(content_chunks)
        msg: dict = {"role": "assistant", "content": content}
        if tool_call_acc:
            msg["tool_calls"] = [
                {
                    "id": acc["id"],
                    "type": acc["type"],
                    "function": {
                        "name": acc["function"]["name"],
                        "arguments": acc["function"]["arguments"],
                    },
                }
                for idx in sorted(tool_call_acc.keys())
                for acc in [tool_call_acc[idx]]
            ]

        # ── 发送 usage 事件（可能为 None） ──
        if final_usage:
            yield StreamEvent(type="usage", data=final_usage)

        yield StreamEvent(type="done", data=msg)

    async def summarize(
        self,
        text: str,
        instruction: str = "请将以下内容压缩为一段连贯的摘要，保留关键信息。用中文，200字以内。只输出摘要。",
        system_prompt: str = "你是一个对话压缩助手，擅长提炼关键信息。",
        max_tokens: int = 1000,
    ) -> str:
        """通用摘要接口"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"{instruction}\n\n{text}"},
        ]
        result = await self.chat(messages, tools=None, max_tokens=max_tokens)
        return result.message.get("content", "").strip()
