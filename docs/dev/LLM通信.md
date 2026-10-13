# LLM 通信

---

## LLM Client (`packages/fp-core/src/fp_core/core/llm_client.py`)

自实现的 OpenAI HTTP 客户端，使用 `httpx.AsyncClient` 直调 OpenAI 格式 API，**无需安装 openai SDK**。

### 设计决策

- 无第三方 SDK 依赖，仅依赖 `httpx`
- 全异步实现（`async/await`）
- 同时支持非流式和流式响应
- 自动处理 `reasoning_content` 和 `<think>` 标签

### Client 类

```python
class Client:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: int = 300,
    )
```

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `api_key` | str | — | API Key |
| `base_url` | str | `"https://api.openai.com/v1"` | API 基础地址（默认指向 OpenAI，但也兼容 DeepSeek 等兼容接口） |
| `timeout` | int | `300` | 总超时秒数（连接超时固定 10s） |

**方法**：

```python
async def close(self)
```

释放 `httpx.AsyncClient` 连接池。应在 Agent 关闭时调用。

**访问路径**：

```python
client = Client(api_key="sk-xxx", base_url="https://api.deepseek.com/v1")
# 通过 client.chat.completions 访问聊天补全 API
response = await client.chat.completions.create(...)
```

---

### chat.completions.create

```python
async def create(
    self,
    model: str,
    messages: list[dict],
    temperature: float | None = None,
    max_tokens: int | None = None,
    tools: list[dict] | None = None,
    extra_body: dict | None = None,
    **kwargs,
) -> CompletionResponse
```

**参数说明**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `model` | str | 模型名称，如 `"deepseek-v4-flash"` |
| `messages` | list[dict] | 消息列表，每项格式 `{"role": "...", "content": "..."}` |
| `temperature` | float \| None | 采样温度，默认由 LLMService 管理 |
| `max_tokens` | int \| None | 最大生成 token 数 |
| `tools` | list[dict] \| None | Function Calling 工具定义列表 |
| `extra_body` | dict \| None | 附加请求体字段，如 `{"enable_thinking": True}` |

**错误处理**：

- `httpx.ConnectError` → `APIError("连接失败: ...")`
- `httpx.TimeoutException` → `APIError("请求超时: ...")`
- HTTP 非 200 状态码 → `APIError("API 返回 {status}: ...")`
- JSON 解析失败 → `APIError("响应 JSON 解析失败: ...")`

---

### 返回类型

#### 非流式 — `CompletionResponse`

```python
class CompletionResponse:
    id: str                    # 响应 ID
    object: str                # 对象类型 ("chat.completion")
    created: int               # 创建时间戳
    model: str                 # 使用的模型
    choices: list[MessageChoice]  # 生成结果列表
    usage: dict                # Token 用量

class MessageChoice:
    index: int                 # 序号
    message: Message           # 消息内容
    finish_reason: str | None  # 结束原因

class Message:
    role: str                  # 角色 ("assistant")
    content: str | None        # 文本内容
    tool_calls: list[ToolCall] | None  # 工具调用列表
    reasoning_content: str | None      # 思考内容（原生的 reasoning_content 字段）

class ToolCall:
    id: str                    # 调用 ID
    type: str                  # 类型 ("function")
    function: ToolCallFunction # 函数调用

class ToolCallFunction:
    name: str                  # 工具名称
    arguments: str             # 参数（JSON 字符串）
```

#### 流式 — `create_stream` + `StreamChunk`

流式接口已**全量实现**（SSE，`text/event-stream`）；Agent 主循环默认走流式路径。
请求体自动带上 `"stream": true` 与 `"stream_options": {"include_usage": true}`
（后者让服务端在流末回传 usage）。

```python
async def create_stream(
    self,
    model: str,
    messages: list[dict],
    temperature: float | None = None,
    max_tokens: int | None = None,
    tools: list[dict] | None = None,
    extra_body: dict | None = None,
    **kwargs,
) -> AsyncIterator[StreamChunk]
```

逐行解析 SSE（`data: {...}`，遇 `data: [DONE]` 结束），逐个 yield `StreamChunk`：

```python
class StreamChunk:
    content: str | None            # 文本 token（delta.content）
    reasoning_content: str | None  # 思考 token（delta.reasoning_content）
    tool_calls: list[dict] | None  # 工具调用增量（delta.tool_calls）
    finish_reason: str | None      # 结束原因
    usage: dict | None             # 用量（开启 include_usage 后于末个 chunk 出现）
```

```python
# 流式用法
async for chunk in client.chat.completions.create_stream(model="...", messages=[...]):
    if chunk.content:
        ...
    if chunk.usage:
        ...
```

> 上层消费入口是 `LLMService.chat_stream()`（把 chunk 归一为 `StreamEvent`），
> 通常不需直接调用 `create_stream`。

### `CompletionResponse` 使用示例

```python
response = await client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello!"},
    ],
    temperature=0.8,
    max_tokens=32768,
)

message = response.choices[0].message
content = message.content          # 回复文本
tool_calls = message.tool_calls    # 工具调用（如果有）
reasoning = message.reasoning_content  # 思考内容（如果有）
```

---

## Think 标签提取

支持两种格式的思考内容提取，优先级顺序：

### 1. 原生 `reasoning_content` 字段（首选）

某些模型（如 DeepSeek R1）原生返回 `reasoning_content` 字段，直接映射到 `Message.reasoning_content`：

```python
# API 返回格式（部分）
{
  "choices": [{
    "message": {
      "content": "最终回复",
      "reasoning_content": "思考过程..."
    }
  }]
}
```

### 2. `<think>...</think>` 标签嵌入 content（fallback）

当模型不支持原生 `reasoning_content` 时，思考过程可能被嵌入到 `content` 字段中，使用 `<think>...</think>` 包裹。

自动提取逻辑：

```python
# 条件：
#   1. reasoning_content 为空
#   2. content 以 "<think>" 开头
#   3. content 包含 "</think>"
# → 提取 <think> 与 </think> 之间的内容 → 存入 reasoning_content
# → content 更新为 </think> 之后的内容（或 None）
```

```python
# 示例
# 输入 content:
#   "<think>用户说hello，我需要回复问候</think>Hello! How can I help you?"

# 提取后:
#   message.reasoning_content = "用户说hello，我需要回复问候"
#   message.content          = "Hello! How can I help you?"
```

如果 `</think>` 后没有内容，`content` 被设为 `None`。

### 使用建议

```python
# 统一获取思考内容
reasoning = message.reasoning_content

# 统一获取回复内容
reply = message.content or "（无文本回复）"
```

---

## 思考内容（reasoning_content）跨轮回传

思考模型（qwen enable_thinking / deepseek v4 thinking 系列）返回的
`reasoning_content` 会随 assistant 消息保留在上下文中，回传策略按请求是否携带 tools 区分：

| 请求形态 | 历史 reasoning_content | 依据 |
|----------|------------------------|------|
| 带 `tools` | **回传**（拼入上下文） | DeepSeek v4 官方要求：工具调用场景需把各轮 CoT 传回 |
| 不带 `tools` | **剥离** | API 会忽略这些字段，回传只浪费 token |

实现位置：
- `llm_service.chat/chat_stream` — done 结果的 assistant_msg 携带 `reasoning_content`；`chat()` 被覆写（测试 mock）时降级为非流式，**先 yield `usage` 事件再 yield `done`**——保证 `ON_AFTER_LLM_CALL` 的逐调用统计字段（F1）在降级路径不被架空
- `agent._invoke_llm` → `add_assistant_message` — 保留进会话状态；持久化（`to_serializable`）同样保留
- `conversation.get_messages_for_llm(with_tools=...)` — 发送前按上表裁剪；agent 主循环以 `bool(tool_definitions)` 传入

轻量任务（摘要 `LLMService.summarize`、webui 会话标题）通过 `config.no_thinking_body()`
强制关闭思考——thinking 模型在非流式 + 小 max_tokens 下会因思考 token 挤占预算而报错或截断。

`summarize` 的隐性调用不经 agent 主循环，usage 曾在此被丢弃（边 #53 面外缺口）。现经
`LLMService(on_usage=...)` 回调回流到 `TokenTracker` 聚合统计（agent 构造时注入
`_record_aux_usage`）；主循环路径由 agent 自行 accumulate、不经过该回调，不会双计。

---

## 超时与瞬时错误重试

无人值守时 LLM API 抖动（连接失败 / 超时 / 限流 / 5xx）不应终结整轮，故调用层内置两件
健壮性机制：

### 超时（`TIMEOUT` → `LLM_TIMEOUT`）

`Agent.__init__` 构造 client 时显式传入超时，故 `TIMEOUT`（顶层键 / provider 级 `timeout`）
真正生效：

```python
self.client = Client(
    api_key=config.LLM_API_KEY,
    base_url=config.LLM_API_BASE_URL,
    timeout=config.LLM_TIMEOUT,   # ← 缺此参数则退回 llm_client 硬编码默认 300s
)
```

（`httpx.Timeout(timeout, connect=10.0)`：总超时与连接超时分开。）

### 瞬时错误重试（`RETRY_COUNT` → `LLM_RETRY_COUNT`）

`LLMConfig.retry_count` 控制失败后的最大重试次数（默认 3，0 = 关闭）。
`LLMService._with_retry`（非流式）与 `chat_stream`（流式）实现有界退避：

| 维度 | 约定 |
|------|------|
| 触发条件 | 仅 `is_retryable(exc)` 为真：`APIError(status_code=0)`（连接失败/超时）、408/409/425/429、5xx |
| 不重试 | 4xx 参数/鉴权类（400/401/403/404/422…）、非 APIError、用户中断 |
| 退避 | 指数 `base·2^n` 封顶 `retry_max_delay`，再乘 `[0.5,1.0]` 抖动（防多实例惊群） |
| 流式特例 | **仅在首个事件产出前**重试；一旦已 yield 过 token 则直接抛（否则与已展示内容重复） |
| 中断安全 | `CancelledError` 是 `BaseException`，`except Exception` 不捕获 → 用户中断从不重试 |
| 可见性 | 重试经 `LLMService(on_retry=...)` 外抛（agent 注入 `_log_llm_retry` → `io.warning`）；回调自身异常被吞，不影响重试 |

调优字段（`LLMConfig`）：`retry_count` / `retry_base_delay`（默认 1.0s）/ `retry_max_delay`（默认 30s）。
重试对 `chat` / `chat_stream` / `summarize` 全部生效（`summarize` 走 `chat`）。
