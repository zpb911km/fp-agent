"""
LLMService — 纯 LLM 调用层

职责：
- 封装 LLM client 调用
- 输入：messages + tools → 输出：LLMResult(message, usage)
- 不处理 IO、不显示 spinner、不格式化输出
- 只做"消息→LLM→响应"的纯转换
- 对**瞬时错误**（连接失败/超时/限流/5xx）做有界退避重试——无人值守时
  API 抖动不应终结整轮；重试次数由 LLMConfig.retry_count 决定

重试不进 IO：可见性经 on_retry 回调外抛给调用方（如 Agent 转 io.warning）。
"""

import asyncio
import contextlib
import random
import types
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fp_core.core.llm_client import APIError

# 可重试的 HTTP 状态码（瞬时/服务端语义）：
#   408 请求超时 / 409 冲突 / 425 过早 / 429 限流；叠加 5xx 段。
# 其余 4xx（400/401/403/404/422…）是参数/鉴权类，重试无意义 → 直接抛。
_RETRYABLE_HTTP_STATUS = frozenset({408, 409, 425, 429})


def is_retryable(exc: BaseException) -> bool:
    """判断 LLM 调用异常是否值得重试。

    - ``APIError(status_code=0)``：连接失败 / 超时（llm_client 的兜底归类）
    - 408 / 409 / 425 / 429：限流与瞬时状态
    - 5xx：服务端错误
    其余异常 → 不重试。

    注：``asyncio.CancelledError`` / ``KeyboardInterrupt`` 是 ``BaseException``，
    本函数只处理 ``Exception``，故用户中断天然不进入重试。
    """
    if isinstance(exc, APIError):
        status = exc.status_code
        return status == 0 or status in _RETRYABLE_HTTP_STATUS or 500 <= status < 600
    return False


@dataclass
class LLMConfig:
    """LLM 配置"""

    model: str = ""
    temperature: float = 0.7
    max_tokens: int = 4096
    extra_body: dict[str, Any] = field(default_factory=lambda: {"enable_thinking": False})

    # ── 重试（瞬时错误） ──────────────────────────────
    retry_count: int = 3  # 失败后最多重试次数（0 = 不重试）
    retry_base_delay: float = 1.0  # 首次重试基础退避（秒），指数增长
    retry_max_delay: float = 30.0  # 单次退避上限（秒）


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
    data: dict[str, Any] | None = None


class LLMService:
    """LLM 调用服务"""

    def __init__(
        self,
        client: Any,
        config: LLMConfig,
        on_usage: Callable[[dict[str, Any] | None, str], None] | None = None,
        on_retry: Callable[[int, int, BaseException, float], None] | None = None,
    ):
        """
        Args:
            client: LLM client 实例（core.llm_client.Client）
                    （注：llm_client.Client 内部成员类型不完整，此处以 Any 兜底）
            config: LLM 配置
            on_usage: 隐性调用（summarize 等不经 agent 主循环的路径）的 usage 回流回调。
                    主循环 chat 路径由 agent 侧累计，故仅 summarize 主动调用，避免双计。
            on_retry: 重试发生时的通知回调 (attempt, max_retries, exc, delay)。
                    仅作可见性外抛；回调自身异常被吞，绝不影响重试主流程。
        """
        self._client: Any = client
        self._config = config
        self._on_usage = on_usage
        self._on_retry = on_retry

    @property
    def model(self) -> str:
        return self._config.model

    # ── 内部：参数解析 / 退避 / 重试 ──────────────────

    def _resolve_params(self, overrides: dict[str, Any]) -> tuple[str, float, int, dict[str, Any]]:
        """把 override 合并进配置（chat 与 chat_stream 共用，避免两份漂移）。"""
        return (
            overrides.get("model", self._config.model),
            overrides.get("temperature", self._config.temperature),
            overrides.get("max_tokens", self._config.max_tokens),
            overrides.get("extra_body", self._config.extra_body),
        )

    def _retry_delay(self, attempt: int, exc: BaseException | None = None) -> float:
        """指数退避 + 抖动；attempt 从 0 计（0 = 首次重试）。

        抖动取 [0.5·delay, 1.0·delay]：避免多实例同时重试造成惊群，
        同时保证间隔下界不至于过密。

        429 走更陡的阶梯（4^attempt 而非 2^attempt）：限流多半是
        **并发槽位型**（槽位被占满，不是时间窗口），短退避等不到空槽。
        首跳仍保持短间隔 —— 429 响应廉价、不消耗 token，早探测几乎无
        成本，槽位刚释放即可立刻接上；其后急剧拉长以撑过占用窗口。
        默认 base=1/cap=30 下总窗口约 10.5~21s（常规 2^attempt 仅 3.5~7s）。
        """
        base = max(0.0, self._config.retry_base_delay)
        cap = max(0.0, self._config.retry_max_delay)
        growth = 4 if (isinstance(exc, APIError) and exc.status_code == 429) else 2
        delay = min(base * (growth**attempt), cap)
        return delay * (0.5 + random.random() * 0.5)

    def _notify_retry(self, attempt: int, retries: int, exc: BaseException, delay: float) -> None:
        if self._on_retry is None:
            return
        with contextlib.suppress(Exception):
            self._on_retry(attempt, retries, exc, delay)

    async def _with_retry(self, make_coro: Callable[[], Awaitable[LLMResult]]) -> LLMResult:
        """非流式调用的有界重试：仅对 is_retryable 的错误退避后重试。"""
        retries = max(0, int(self._config.retry_count))
        attempt = 0
        while True:
            try:
                return await make_coro()
            except Exception as e:  # CancelledError 是 BaseException → 不被捕获，用户中断不重试
                if attempt >= retries or not is_retryable(e):
                    raise
                delay = self._retry_delay(attempt, e)
                self._notify_retry(attempt + 1, retries, e, delay)
                await asyncio.sleep(delay)
                attempt += 1

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **overrides: Any,
    ) -> LLMResult:
        """
        调用 LLM 并返回 LLMResult(message, usage)（瞬时错误自动退避重试）。

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
        model, temperature, max_tokens, extra_body = self._resolve_params(overrides)
        return await self._with_retry(
            lambda: self._chat_once(messages, tools, model, temperature, max_tokens, extra_body)
        )

    async def _chat_once(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        temperature: float,
        max_tokens: int,
        extra_body: dict[str, Any],
    ) -> LLMResult:
        """单次非流式调用（不含重试）。"""
        kwargs: dict[str, Any] = {
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

        msg: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
        if getattr(message, "reasoning_content", None):
            # 思考模型（qwen/deepseek v4 等）的思维链，随消息保留供回传上下文
            msg["reasoning_content"] = message.reasoning_content
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
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **overrides: Any,
    ) -> AsyncIterator[StreamEvent]:
        """
        流式调用 LLM，逐个 yield StreamEvent。

        事件序列（典型）:
          1. reasoning_token → reasoning_token → ...  （思考过程，可选）
          2. content_token → content_token → ...      （回复文本）
          3. tool_call → ...                           （工具调用，可选）
          4. usage                                     （用量）
          5. done                                      （完整消息）

        调用方在 done 事件中获取完整的 assistant_msg。

        重试语义：仅当**首个事件产出之前**失败才退避重试；一旦已 yield 过
        任何 token/事件，重试会与已展示内容重复，故直接抛出（由上层负责）。

        向后兼容：若 chat() 被覆写（如测试 mock），自动降级为
        非流式调用并 yield 单个 done 事件。
        """
        # ── 向后兼容：检测 chat() 是否被覆写（如测试 mock） ──
        bound_chat = self.chat
        if not isinstance(bound_chat, types.MethodType) or bound_chat.__func__ is not LLMService.chat:
            result = await self.chat(messages, tools=tools, **overrides)
            # 降级路径同样下发 usage 事件 — 否则 F1 字段（逐调用统计）在 mock/降级场景被架空
            if result.usage:
                yield StreamEvent(type="usage", data=result.usage)
            yield StreamEvent(type="done", data=result.message)
            return

        model, temperature, max_tokens, extra_body = self._resolve_params(overrides)
        retries = max(0, int(self._config.retry_count))
        attempt = 0
        while True:
            yielded = False
            try:
                async for event in self._stream_once(messages, tools, model, temperature, max_tokens, extra_body):
                    yielded = True
                    yield event
                return
            except Exception as e:  # CancelledError 是 BaseException → 用户中断不重试
                if yielded or attempt >= retries or not is_retryable(e):
                    raise
                delay = self._retry_delay(attempt, e)
                self._notify_retry(attempt + 1, retries, e, delay)
                await asyncio.sleep(delay)
                attempt += 1

    async def _stream_once(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        temperature: float,
        max_tokens: int,
        extra_body: dict[str, Any],
    ) -> AsyncIterator[StreamEvent]:
        """单次流式调用（不含重试）；yield 与 chat_stream 同构的事件序列。"""
        kwargs: dict[str, Any] = {
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
        tool_call_acc: dict[int, dict[str, Any]] = {}  # index → {id, type, function: {name, arguments}}

        async for chunk in self._client.chat.completions.create_stream(**kwargs):
            # ── 文本 token ──
            # 空 delta（content=""）不产事件：provider 在流首/工具轮常发空帧，
            # 下传会被前端当成「正文已到」→ 思考栏提前折叠、空卡片凭空出现
            if chunk.content:
                content_chunks.append(chunk.content)
                yield StreamEvent(type="content", text=chunk.content)

            # ── 思考 token ──
            # 同上：reasoning_content="" 是空帧而非思考内容，不产事件
            if chunk.reasoning_content:
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
                yield StreamEvent(type="usage", data=chunk.usage)

        # ── 构造完整 assistant_msg ──
        content = "".join(content_chunks)
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if reasoning_chunks:
            # 思维链保留在消息上：带 tools 的请求需回传（DeepSeek v4 官方要求）
            msg["reasoning_content"] = "".join(reasoning_chunks)
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

        yield StreamEvent(type="done", data=msg)

    async def summarize(
        self,
        text: str,
        instruction: str = "请将以下内容压缩为一段连贯的摘要，保留关键信息。用中文，200字以内。只输出摘要。",
        system_prompt: str = "你是一个对话压缩助手，擅长提炼关键信息。",
        max_tokens: int = 1000,
    ) -> str:
        """通用摘要接口

        轻量任务，强制关闭思考：thinking 模型（qwen/deepseek v4）在非流式
        + 小 max_tokens 下会因思考 token 挤占预算而报错或截断。
        参数格式由 config.no_thinking_body() 按激活 provider 选择。
        """
        from fp_core.config import no_thinking_body

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"{instruction}\n\n{text}"},
        ]
        result = await self.chat(messages, tools=None, max_tokens=max_tokens, extra_body=no_thinking_body())
        # #53 面外缺口：隐性调用的 usage 此前在此被丢弃（连聚合统计都没进）——回流给调用方注入的回调
        if self._on_usage is not None:
            self._on_usage(result.usage, self.model)
        return result.message.get("content", "").strip()
