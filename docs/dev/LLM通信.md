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

#### 流式 — `SSEIterator`

（当前版本仅支持非流式，流式接口预留。）

```python
# 流式用法示意（future）
async for chunk in client.chat.completions.create(stream=True, ...):
    # Chunk 包含 delta.content / delta.tool_calls / finish_reason
    ...
```

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
