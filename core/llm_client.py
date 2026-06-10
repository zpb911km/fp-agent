"""
OpenAI API HTTP 客户端替代模块
================================
使用 httpx 库直接调用 OpenAI 格式的 API，无需安装 openai SDK。
仅支持非流式调用（全异步版本）。

Usage:
    import openai
    client = openai.Client(api_key="...", base_url="...")

    response = await client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": "Hello"}],
        temperature=0.8,
        max_tokens=32768,
        tools=[...],
        extra_body={"enable_thinking": False},
    )
    print(response.choices[0].message.content)
"""

import contextlib
import json
from typing import Any

import httpx

# ═══════════════════════════════════════════════════════════════
# 异常类
# ═══════════════════════════════════════════════════════════════


class APIError(Exception):
    """OpenAI API 错误，兼容 openai.APIError"""

    def __init__(self, message: str, status_code: int = 0, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


# ═══════════════════════════════════════════════════════════════
# 非流式响应对象模型
# ═══════════════════════════════════════════════════════════════


class ToolCallFunction:
    __slots__ = ("name", "arguments")

    def __init__(self, data: dict):
        self.name: str = data.get("name", "")
        self.arguments: str = data.get("arguments", "")


class ToolCall:
    __slots__ = ("id", "type", "function")

    def __init__(self, data: dict):
        self.id: str = data.get("id", "")
        self.type: str = data.get("type", "function")
        self.function = ToolCallFunction(data.get("function", {}))


class Message:
    """非流式响应中的 message"""

    __slots__ = ("role", "content", "tool_calls", "reasoning_content")

    def __init__(self, data: dict):
        self.role: str = data.get("role", "assistant")
        self.content: str | None = data.get("content")
        self.reasoning_content: str | None = data.get("reasoning_content")

        # 非流式场景下的 <think> 标签提取
        # 仅在原生无 reasoning_content 且 content 以 <think> 开头时触发
        if (
            not self.reasoning_content
            and self.content
            and self.content.startswith("<think>")
            and "</think>" in self.content
        ):
            close_idx = self.content.index("</think>")
            self.reasoning_content = self.content[7:close_idx]
            after = self.content[close_idx + 8 :]
            self.content = after if after else None
        raw_tool_calls = data.get("tool_calls")
        if raw_tool_calls:
            self.tool_calls = [ToolCall(tc) for tc in raw_tool_calls]
        else:
            self.tool_calls = None


class MessageChoice:
    __slots__ = ("index", "message", "finish_reason")

    def __init__(self, data: dict):
        self.index: int = data.get("index", 0)
        self.message = Message(data.get("message", {}))
        self.finish_reason: str | None = data.get("finish_reason")


class CompletionResponse:
    """非流式完整响应"""

    __slots__ = ("id", "object", "created", "model", "choices", "usage")

    def __init__(self, data: dict):
        self.id: str = data.get("id", "")
        self.object: str = data.get("object", "chat.completion")
        self.created: int = data.get("created", 0)
        self.model: str = data.get("model", "")
        raw_choices = data.get("choices", [])
        self.choices = [MessageChoice(c) for c in raw_choices]
        self.usage = data.get("usage")


# ═══════════════════════════════════════════════════════════════
# 核心 Client（异步版本）
# ═══════════════════════════════════════════════════════════════


class Completions:
    """client.chat.completions"""

    def __init__(self, client: "Client"):
        self._client = client

    async def create(
        self,
        model: str,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        extra_body: dict | None = None,
        **kwargs,
    ) -> "CompletionResponse":
        """
        发起聊天补全请求（异步，非流式）。
        """
        url = f"{self._client.base_url}/chat/completions"
        headers = self._client._headers()

        # 构建请求体
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }

        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if tools is not None:
            body["tools"] = tools
        if extra_body:
            body.update(extra_body)

        try:
            resp = await self._client._session.post(
                url,
                headers=headers,
                json=body,
            )
        except httpx.ConnectError as e:
            raise APIError(f"连接失败: {e}", status_code=0) from None
        except httpx.TimeoutException as e:
            raise APIError(f"请求超时: {e}", status_code=0) from None

        # 处理 HTTP 错误
        if resp.status_code != 200:
            error_body = ""
            with contextlib.suppress(Exception):
                error_body = resp.text
            raise APIError(
                f"API 返回 {resp.status_code}: {error_body}",
                status_code=resp.status_code,
                body=error_body,
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise APIError(f"响应 JSON 解析失败: {e}", status_code=resp.status_code) from None
        return CompletionResponse(data)


class Chat:
    """client.chat"""

    def __init__(self, client: "Client"):
        self.completions = Completions(client)


class Client:
    """
    OpenAI API 客户端（异步版本）。

    用法:
        client = Client(api_key="sk-xxx", base_url="https://api.openai.com/v1")
        response = await client.chat.completions.create(model="...", messages=[...])
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: int = 300,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._session = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=True,
        )
        self._timeout = timeout
        self.chat = Chat(self)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def close(self):
        """释放连接池"""
        await self._session.aclose()
