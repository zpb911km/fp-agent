"""
LLMService — 纯 LLM 调用层

职责：
- 封装 LLM client 调用
- 输入：messages + tools → 输出：LLMResult(message, usage)
- 不处理 IO、不显示 spinner、不格式化输出
- 只做"消息→LLM→响应"的纯转换
"""

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
