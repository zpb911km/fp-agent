"""
OpenAI API HTTP 客户端替代模块
================================
使用 requests 库直接调用 OpenAI 格式的 API，无需安装 openai SDK。
支持流式和非流式两种调用方式，完全兼容 agent.py 的现有接口。

Usage:
    import openai
    client = openai.Client(api_key="...", base_url="...")
    
    # 流式调用
    response = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": "Hello"}],
        stream=True,
        temperature=0.8,
        max_tokens=32768,
        tools=[...],
        extra_body={"enable_thinking": False},
    )
    for chunk in response:
        print(chunk.choices[0].delta.content)
    
    # 非流式调用
    response = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": "Hello"}],
        stream=False,
    )
    print(response.choices[0].message.content)
"""

import json
import sys
from typing import Any, Iterator, Optional

import requests


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
# 流式 Chunk 对象模型 — 模仿 openai SDK 的响应结构
# ═══════════════════════════════════════════════════════════════

class ToolCallFunctionDelta:
    """tool_call 中的 function 增量"""
    __slots__ = ("name", "arguments")
    
    def __init__(self, data: dict):
        self.name: str = data.get("name", "")
        self.arguments: str = data.get("arguments", "")


class ToolCallDelta:
    """流式响应中的单个 tool_call 增量"""
    __slots__ = ("index", "id", "type", "function")
    
    def __init__(self, data: dict):
        self.index: int = data.get("index", 0)
        self.id: Optional[str] = data.get("id")
        self.type: Optional[str] = data.get("type")
        func_data = data.get("function", {})
        self.function = ToolCallFunctionDelta(func_data)


class Delta:
    """流式 chunk 中的 delta 字段"""
    __slots__ = ("content", "reasoning_content", "tool_calls", "role")
    
    def __init__(self, data: dict):
        self.content: Optional[str] = data.get("content")
        self.reasoning_content: Optional[str] = data.get("reasoning_content")
        self.role: Optional[str] = data.get("role")
        raw_tool_calls = data.get("tool_calls")
        if raw_tool_calls:
            self.tool_calls = [ToolCallDelta(tc) for tc in raw_tool_calls]
        else:
            self.tool_calls = None


class Choice:
    """流式 chunk 中的 choices 元素"""
    __slots__ = ("delta", "index", "finish_reason")
    
    def __init__(self, data: dict):
        self.delta = Delta(data.get("delta", {}))
        self.index: int = data.get("index", 0)
        self.finish_reason: Optional[str] = data.get("finish_reason")


class Chunk:
    """流式响应的一个 chunk —— 可被 for 循环迭代"""
    __slots__ = ("id", "object", "created", "model", "choices", "usage")
    
    def __init__(self, data: dict):
        self.id: str = data.get("id", "")
        self.object: str = data.get("object", "chat.completion.chunk")
        self.created: int = data.get("created", 0)
        self.model: str = data.get("model", "")
        raw_choices = data.get("choices", [])
        self.choices = [Choice(c) for c in raw_choices]
        self.usage = data.get("usage")


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
        self.content: Optional[str] = data.get("content")
        self.reasoning_content: Optional[str] = data.get("reasoning_content")
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
        self.finish_reason: Optional[str] = data.get("finish_reason")


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
# SSE 流式解析器
# ═══════════════════════════════════════════════════════════════

class SSEIterator:
    """
    Server-Sent Events 流式迭代器。
    将 requests 的流式响应按 'data: ' 行解析，产出 Chunk 对象。
    """
    
    def __init__(self, response: requests.Response):
        self._response = response
        self._iterator = response.iter_lines(decode_unicode=True)
    
    def __iter__(self) -> Iterator[Chunk]:
        return self._parse()
    
    def _parse(self) -> Iterator[Chunk]:
        try:
            for line in self._iterator:
                if not line:
                    continue
                # 跳过非 data: 开头的行（如注释、空行）
                if line.startswith("data: "):
                    data_str = line[6:]
                elif line.startswith("data:"):
                    data_str = line[5:]
                else:
                    continue
                
                # 流结束标记
                if data_str.strip() == "[DONE]":
                    return
                
                try:
                    data = json.loads(data_str)
                    yield Chunk(data)
                except json.JSONDecodeError:
                    # 某些 API 可能在末尾发送非 JSON 数据，忽略
                    continue
        
        except (requests.ConnectionError, requests.ChunkedEncodingError) as e:
            raise APIError(f"流式连接中断: {e}", status_code=0)
        except Exception as e:
            raise APIError(f"流式解析异常: {e}", status_code=0)
        finally:
            self._response.close()


# ═══════════════════════════════════════════════════════════════
# 核心 Client
# ═══════════════════════════════════════════════════════════════

class Completions:
    """client.chat.completions"""
    
    def __init__(self, client: "Client"):
        self._client = client
    
    def create(
        self,
        model: str,
        messages: list[dict],
        stream: bool = False,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[list[dict]] = None,
        extra_body: Optional[dict] = None,
        **kwargs,
    ) -> Any:
        """
        发起聊天补全请求。
        
        参数完全对齐 openai SDK 的 client.chat.completions.create。
        
        Returns:
            - stream=True 时返回 SSEIterator (可迭代 Chunk)
            - stream=False 时返回 CompletionResponse
        """
        url = f"{self._client.base_url}/chat/completions"
        headers = self._client._headers()
        
        # 构建请求体
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
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
            resp = self._client._session.post(
                url,
                headers=headers,
                json=body,
                stream=stream,
                timeout=(10, 300),  # (连接超时, 读取超时)
            )
        except requests.ConnectionError as e:
            raise APIError(f"连接失败: {e}", status_code=0)
        except requests.Timeout as e:
            raise APIError(f"请求超时: {e}", status_code=0)
        
        # 处理 HTTP 错误
        if resp.status_code != 200:
            error_body = ""
            try:
                error_body = resp.text
            except Exception:
                pass
            raise APIError(
                f"API 返回 {resp.status_code}: {error_body}",
                status_code=resp.status_code,
                body=error_body,
            )
        
        if stream:
            return SSEIterator(resp)
        else:
            try:
                data = resp.json()
            except json.JSONDecodeError as e:
                raise APIError(f"响应 JSON 解析失败: {e}", status_code=resp.status_code)
            return CompletionResponse(data)


class Chat:
    """client.chat"""
    
    def __init__(self, client: "Client"):
        self.completions = Completions(client)


class Client:
    """
    OpenAI API 客户端。
    
    用法:
        client = Client(api_key="sk-xxx", base_url="https://api.openai.com/v1")
        response = client.chat.completions.create(model="...", messages=[...], stream=True)
    """
    
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: int = 300,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._timeout = timeout
        self.chat = Chat(self)
    
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    
    def close(self):
        """释放连接池"""
        self._session.close()
