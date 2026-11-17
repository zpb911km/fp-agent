# ACP 协议

> ACP（Agent Communication Protocol）是本项目的 JSON-RPC 2.0 远程调用协议，
> 由 `fp-acp` 子包实现。
>
> **静默约定**：`ACPServer` 构造期以 `os.environ.setdefault` 置 `FP_QUIET=1`，
> 不渲染 Logo/提示（`stdout` 只许出 JSON-RPC 报文）。变量名引自
> `fp_core.api.spawn.QUIET_ENV` 而非硬编码——quiet/silent 这族跨层环境变量统一由
> spawn 契约模块供给，避免「改名漏一处」的静默失效。用户显式设置优先。

## 📄 文档

ACP 的完整文档（安装、接口定义、调用示例、使用场景）位于 **子包源文件**：

👉 **[packages/fp-acp/README.md](../../packages/fp-acp/README.md)**

## ❓ 交互问答（ask_user → elicit / deferred）

ACP 的 `ask_user` 按**客户端能力协商**分两条路径（实现：`packages/fp-acp/src/fp_acp/server.py`）：

### ① 带内 elicit（客户端声明 `elicitation.form`）
`initialize` 时记录 `clientCapabilities.elicitation`（`form`/`url` **各自显式存在且非 null** 才算支持）。
声明了 `form` 时，`ACPIO.ask()` 发标准 **`elicitation/create`（form mode）** 带内请求并等响应：

- `prompt` → `message`；`options` → schema 字段 `answer` 的 `enum`（天然渲染为选项）；
  `suggest` 命中 `enum` 时 → `default`（预填/高亮）；
- 响应三态：`accept` → 回填 `content.answer`（带内回流，`ask_deferred=False`）；
  `decline`/`cancel` → 返回空串（用户显式拒绝/关闭，**不伪造作答**）；
- 规范硬约束：Agent **MUST NOT** 请求客户端未声明的 mode、**MUST NOT** 假设成功 →
  请求/协议异常时**优雅回退**到 ②。

### ② deferred 回退（未声明 `elicitation.form`，或 elicit 失败）
`ACPIO.ask_deferred = True`：

- `ask_user` 工具调用时，问题（含 `options` 编号列表与 `suggest` 推荐值）被
  推送为一条 `agent_message_chunk` 展示在 IDE 会话中，`ask()` **立即返回空串**；
- 编排层识别 deferred → 工具返回 `{"status":"deferred","note":"…下一条消息即为其回答"}`，
  **不注入、不回执**，LLM 结束本轮等待；
- 用户在 IDE 对话框的**下一条消息**即其回答，自然走新轮次 user 消息（权威注入，I2）。

> 旧实现 `ask()` 返回 `"q"`，会被当成用户回答注入上下文 —— 伪造人类发言，
> 已修复（测试：`test_ask_user.py::test_ask_deferred_no_injection`）。
> elicit 路径测试：`packages/fp-acp/tests/test_elicitation.py`。

## ✅ 协议合规基线（ACP v1）

**版本**：本端声明并支持 **v1**（`_SUPPORTED_PROTOCOL_VERSIONS = (1,)`）。
v2 目前是 Draft，客户端请求 v2 时按协商规则回退到 v1（客户端自行决定是否继续），
不在未实现的情况下假装支持。

**已实现（有测试守护，`tests/test_acp_wire.py`）**：

| 能力 | 说明 |
|---|---|
| 版本协商 | `initialize` 回显可支持版本，`agentInfo.version` 取包元数据 |
| `session/new` + `cwd` | `cwd` 为 required，作为会话根生效（`chdir`），缺失 → `-32602` |
| `session/load` 历史回放 | 响应**之前**用 `user_message_chunk`/`agent_message_chunk` 回放全量历史；会话不存在 → `-32602`；已 `session/new` 但未落盘的内存态空会话可正常 load（回放为空） |
| `resource_link` | baseline MUST，转为「仅引用未附带内容」提示，不再静默丢弃 |
| `ToolKind` | 只发 schema 枚举内取值（`bash→execute`、未知→`other`） |
| `ToolCallContent` | 统一 `{"type":"content"/"diff"/"terminal"}` union 包装，`diff.path` 绝对化 |
| `rawInput` | 传 object（原始参数），不再传格式化字符串；不填 `rawOutput`（工具结果是字符串） |
| `toolCallId` | 按 fp-core 传入的真实 call id 配对，并行同名工具不错配 |
| `messageId` | 整轮共享一个 id（id 变化 = 新消息开始） |
| `plan` 生命周期 | `pending/in_progress/completed` 三态，结束时发空 `entries` 收尾 |
| 错误语义 | 并发 prompt → `-32600`（不用 `stopReason:"cancelled"`，那是 `session/cancel` 专属） |

**未实现（已知缺口，二期）**：

- 多会话模型：`_session_id` 仍是单例，第二個 `session/new` 会顶掉第一个；
  `session/prompt` 已校验 `sessionId` 防串话，但并行会话尚不支持。
- `session/request_permission`：IDE 的「危险命令确认」UI 不生效。
- `usage_update`（上下文用量/费用）、`session/set_config_option`、`session/delete|resume|close`、
  `elicitation` 之外的 `logout`。
- `session/list` 的 `cwd` **过滤**与 `cursor` 分页：会话 meta 尚未持久化 `cwd`，
  目前只接受参数并回填，返回全量会话。
- `session/set_mode`：未声明 `modes/availableModes`，故**不提供**伪实现（调用回 `-32601`）。

> 纪律：这些是「线上形状」硬约束，改动 `server.py` 的相关分支必须同步
> `tests/test_acp_wire.py`，否则接口漂移不可观测（历史上同类修复曾被一次重复的
> `def send` 静默抵消）。规范：https://agentclientprotocol.com/protocol/v1

## 🔗 快速链接

- [安装与使用](../../packages/fp-acp/README.md#快速使用)
- [JSON-RPC 接口](../../packages/fp-acp/README.md#json-rpc-接口)
- [调用示例](../../packages/fp-acp/README.md#调用示例)
- [使用场景](../../packages/fp-acp/README.md#使用场景)

## 🔄 reload 与会话续接

ACP 实例经 `reload` 热重启后自动恢复会话，并按 handoff 的 `kind` 分流
（`fp-acp` 的 `main()` 首行已捕获启动命令快照 `FP_LAUNCH_JSON`——新入口契约
第 0 步 `capture_launch_command()`，故直启 `fp-acp` 亦可 execve 重启）：

- **kind=tool**（LLM 携动态口令发起）：自动续接对话，续接完成后为挂起的
  `session/prompt` 请求补发 chunk 通知与带 `"stopReason": "end_turn"` 的 result；
- **kind=command**（用户输入 `/reload`，人触发免口令）：不进续接，仅恢复会话
  并显示一行完成提示，同样为挂起请求补发 result（否则客户端悬等）。

两种 kind 客户端都无需重发请求。

**连接级能力跨 exec 保留**：reload 是 `execve` 重启进程（内存态全丢），而客户端的
stdio 连接保持不变、**不会重发 `initialize`** —— 故 `initialize` 协商的
`clientCapabilities` 会写入环境变量 `FP_ACP_CLIENT_CAPS` 随 exec 继承，新实例
构造时即恢复。这样 `ask_user` 的**带内 elicit 不会因 reload 静默失效**（否则能力
回退为默认 `form=False`，只剩 deferred 文本路径）。载体模式同 `FP_ACP_PENDING_REQ`。

> 注：能力只在被协商过之后才可继承——若某新实例自始至终未收到 `initialize`
> （如客户端刚重连即被 exec），环境变量尚不存在，此时只能退回 deferred，直到
> 客户端下一次 `initialize`。
