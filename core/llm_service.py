"""
LLMService — 纯 LLM 调用层

职责：
- 封装 LLM client 调用
- 输入：messages + tools → 输出：assistant message dict
- 不处理 IO、不显示 spinner、不格式化输出
- 只做"消息→LLM→响应"的纯转换
"""

from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field


@dataclass
class LLMConfig:
    """LLM 配置"""
    model: str = ""
    temperature: float = 0.7
    max_tokens: int = 4096
    extra_body: Dict = field(default_factory=lambda: {"enable_thinking": False})


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
        messages: List[Dict],
        tools: Optional[List[Dict]] = None,
        **overrides,
    ) -> Dict:
        """
        调用 LLM 并返回 assistant message dict。
        
        Args:
            messages: 消息列表
            tools: 工具定义列表（可选）
            **overrides: 覆盖 LLMConfig 中的字段（model, temperature, max_tokens 等）
            
        Returns:
            assistant message dict:
            {
                "role": "assistant",
                "content": "...",
                "tool_calls": [...]  # 如果有
            }
        """
        model = overrides.get("model", self._config.model)
        temperature = overrides.get("temperature", self._config.temperature)
        max_tokens = overrides.get("max_tokens", self._config.max_tokens)
        extra_body = overrides.get("extra_body", self._config.extra_body)

        kwargs = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
        if extra_body:
            kwargs["extra_body"] = extra_body

        response = await self._client.chat.completions.create(**kwargs)
        message = response.choices[0].message

        msg: Dict = {"role": "assistant", "content": message.content or ""}
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

        return msg

    async def summarize(
        self,
        text: str,
        instruction: str = "请将以下内容压缩为一段连贯的摘要，保留关键信息。用中文，200字以内。只输出摘要。",
        system_prompt: str = "你是一个对话压缩助手，擅长提炼关键信息。",
    ) -> str:
        """通用摘要接口"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"{instruction}\n\n{text}"},
        ]
        result = await self.chat(messages, tools=None, max_tokens=500)
        return result.get("content", "").strip()
