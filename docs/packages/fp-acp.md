# fp-acp — ACP（Agent Client Protocol）适配层

[![PyPI](https://img.shields.io/pypi/v/fp-acp.svg)](https://pypi.org/project/fp-acp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](../../LICENSE)

> 把 FP 暴露为一个 ACP v1 服务端——编辑器（Zed / VS Code 等）作为 ACP 客户端，
> 经 **stdio 上的 JSON-RPC 2.0** 驱动 FP 对话，一行一条报文，不监听任何端口。

## 安装

```bash
pip install fp-agent[acp]
```

只装适配层本体亦可：`pip install fp-acp`（`fp-core` 会被一并拉入）。

## 启动

由 IDE 侧配置为「ACP Agent 命令」，两种等价入口：

```bash
fp --mode acp        # 伞包入口（fp-agent[acp]）
fp-acp               # 子包自带的 console_script
```

两者 `main()` **均不接受任何命令行参数**：进程的 `stdin` 就是协议通道，`stdout` 只输出
JSON-RPC 报文（FP 自身日志与显示层被重定向到 `stderr`，故 IDE 日志面板可见调试信息而不污染协议流）。

- 离开 IDE 直接手敲 `fp-acp` 会看到它静待 `stdin` 输入——这是正常行为，不是卡死。
- 无 TCP 监听、无 `--host/--port`、无 `tcp://`：安全性由「只能被父进程通过管道驱动」保证。
- 构造期以 `os.environ.setdefault` 置 `FP_QUIET=1`（不渲染 Logo/提示）。变量名取自
  `fp_core.api.spawn.QUIET_ENV`，**不硬编码字符串**——quiet/silent 这族变量原先散落在
  各前端各写一遍，改名必漏一处，故统一收进 spawn 契约模块（该模块是 L3 前端 ↔ L4 资产
  的公开接口）。用户的显式设置优先（`setdefault`，不自作主张覆盖）。

## 协议

JSON-RPC 2.0 over stdio，**一行一条**（`\n` 分隔，不允许跨行美化打印）；字段名遵循 ACP v1 的
camelCase（`sessionId` / `agentCapabilities` / `agentInfo`），`protocolVersion` 是**整数** `1`。

| 方法 | 类型 | 必需参数 | 返回 `result` |
|---|---|---|---|
| `initialize` | 请求 | `protocolVersion`（可选 `clientInfo` `clientCapabilities`） | `protocolVersion` `agentCapabilities` `agentInfo` `authMethods` |
| `session/new` | 请求 | `cwd`（可选 `mcpServers`） | `sessionId` |
| `session/load` | 请求 | `sessionId` `cwd`（可选 `mcpServers`） | `sessionId`（**响应前**回放全量历史） |
| `session/list` | 请求 | `cwd` | `sessions[]` |
| `session/close` | 请求 | `sessionId` | `{}`（释放本连接侧状态，**不删磁盘历史**） |
| `session/prompt` | 请求 | `sessionId` `prompt[]`（兼容 `messages[]`） | `stopReason` |
| `session/commands` | 请求 | — | `commands[]`（`name` 不含 `/` 前缀） |
| `session/ping` | 请求 | — | `pong: true` |
| `session/cancel` | **通知** | `sessionId` | 无响应 |
| `initialized` | 通知 | — | 无响应（仅记日志） |

Agent → 客户端的推送与请求（同一条 `stdout` 上反向流动）：

- `session/update` 通知：`sessionUpdate` 取 `agent_message_chunk` / `tool_call` /
  `tool_call_update` / `plan` / `available_commands_update` / `compaction_update` /
  `session_info_update`（后两者见下文「上下文改写与标题通知」，均按客户端能力门控）；
- `elicitation/create` 请求：仅当客户端在 `initialize` 中声明了
  `clientCapabilities.elicitation.form` 时才发起（`ask_user` 的带内问答）。

**版本协商**：客户端声明 `protocolVersion`，本端只支持 `1`（v2 仍是 Draft）——
匹配则回显 `1`；请求更高版本时回 `1`，是否继续由客户端决定，绝不假装支持。

`initialize` 声明出的能力即真实边界：

```json
{"protocolVersion": 1,
 "agentCapabilities": {"loadSession": true,
   "sessionCapabilities": {"list": {}, "close": {}},
   "promptCapabilities": {"image": false, "audio": false, "embeddedContext": true},
   "mcpCapabilities": {"http": false, "sse": false}},
 "agentInfo": {"name": "fp", "title": "FP", "version": "<包元数据>"},
 "authMethods": []}
```

`authMethods` 为空 = 不依赖客户端认证；`image`/`audio` 为 `false` = 不接受图片/音频块；
`embeddedContext: true` = 支持 `resource` / `resource_link` 块。

## 会话语义

- **`cwd` 是会话根（会话属性，不是进程状态）**：`session/new` 与 `session/load` 都要求绝对路径 `cwd`，
  服务端把它登记进会话表，**不调 `os.chdir`**。转 turn 时由 `fp_core.core.cwd` 的
  `ContextVar` 承载基准目录（asyncio 按 Task 隔离），于是 bash 的 `cwd=`、文件工具的
  相对路径解析都落在**该会话自己的**项目目录内（绝对路径不受影响）。
  这是防止「在项目 A 对话、却写到 agent 进程自身目录」的关键，也使得两个会话并存时
  互不污染。`cwd` 缺失/目录不存在 → `-32602`。
  绑定动作其实由 **core** 完成：`SessionManager` 装载会话时就把基准切到 `meta.cwd`
  （`_bind_session_cwd`），所以 CLI `/resume`、WebUI 切会话同样落在会话自己的根目录。
  ACP 只在**一种**情况下覆盖——客户端在 `session/load` 声明的 cwd 与落盘 meta 不一致时
  以客户端为准，且仅本轮有效（turn 结束复位）。
- **`session/new`** 建会话后**先回 `sessionId`，再**推 `available_commands_update`——
  顺序不能反：IDE 要先拿到 `sessionId` 才能归属命令列表。未注册的 `/` 命令会被 IDE 拦截。
- **`session/load`** 按规范 MUST 在响应**之前**用 `user_message_chunk` / `agent_message_chunk`
  回放历史；会话不存在 → `-32602`（不静默新建空会话）。刚 `session/new` 尚未落盘的内存态空会话可 load，回放为空。
- **`session/prompt`** 的 `result` **只含 `stopReason`**（`end_turn`；被取消则 `cancelled`），
  正文一律走 `session/update` 的 `agent_message_chunk` 通知。一轮内所有 chunk 共享同一
  `messageId`（id 变化 = 新消息开始）。同一时刻只允许一个 prompt 在跑，并发请求 →
  `-32600`（不用 `cancelled` 冒充，那是 `session/cancel` 的专属语义）。
- **`session/cancel`** 是通知：立即取消当前 prompt task，不等下一个中断检查点。
- **多会话并存**：同一连接维护 `_sessions[sid]` 会话表，第二个 `session/new` **不会顶掉**第一个；
  `session/prompt` 遇到未知 `sessionId` → `-32602`；turn 内所有通知都带**该会话自己的** sid；
  `session/cancel` 只作用于目标会话。
- **`mcpServers` 是会话私有**：`session/new` / `session/load` 里的 `mcpServers` 只对**声明它的
  那条会话**可见（IDE 的 Settings → MCP Servers 配的 server 不会污染其它 thread）。
  可见性由插件侧按执行期会话过滤（与 `cwd` 同一套 contextvar 范式），`mcp_list` / `mcp_call`
  也按会话裁决，模型在 B 会话里点名 A 的 server 会被拒绝。仅支持 **stdio** 传输；
  `http`/`sse` 型会被**明确拒绝并记日志**（不静默丢弃）。同名 server 在两会话里互不覆盖。
- **`session/close`** 释放本连接侧状态与该会话的私有 MCP 子进程，**不删磁盘历史**
  （之后仍可 `session/load` 打开），且**幂等**。会话表另有上限 `_MAX_SESSIONS`（64），
  超出时淘汰最久未用者（跳过当前聚焦与正在输出的会话，一并回收其私有 server）——
  防长连接跑几十个 thread 时登记只增不减。
- **上下文改写与标题通知**：客户端在 `initialize` 声明 `clientCapabilities.session.compaction`
  （**必须是对象**，`true`/字符串等噪声一律视为未声明）时，`/compact` 会推
  `compaction_update`（`in_progress` → `completed`|`failed`|`cancelled`，开态与终态**同
  `compactionId`**）。是否真压缩按**上下文指纹**（条数 + 内容哈希）判定：`handled` 但内容
  一字未改 → `cancelled`，不伪造 `completed`；条数不变而内容被改写（小历史边界上的真压缩）
  仍判 `completed`。摘要文本恒省略（`compaction_summary_chunk` 不发），避免与模型实际看到
  的上下文不一致。会话标题（`meta.summary`）在一轮结束时真变了才推 `session_info_update`，
  `updatedAt` 为带时区的 ISO 8601。
- **跨会话并发 = 排队**：`_turn_lock` 串行占用 core 的唯一执行槽，两个会话同时要跑时第二个排队等待
  （规范只要求「会话可并存」，没有「必须并发」的 MUST）。**同会话**并发 prompt 仍是客户端 bug → `-32600`。
  真并发（每会话独立 Agent / 进程级隔离）未做，属二期。
- **reload 续接**：`reload` 经 `execve` 重启而 stdio 连接不变，客户端不会重发 `initialize`——
  协商到的客户端能力存入 `FP_ACP_CLIENT_CAPS` 随 exec 继承，挂起的 `session/prompt` 由新实例补发 chunk
  与 `stopReason`，客户端无需重试。详见索引文档。

## 示例

最小一轮（`$` 为服务端 `stdout` 推送，其余为客户端写入的 `stdin`）：

```jsonc
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientInfo":{"name":"zed","version":"0.170.0"},"clientCapabilities":{"elicitation":{"form":{}}}}}
{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"/home/me/proj"}}
$ {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1,"agentCapabilities":{"loadSession":true,"sessionCapabilities":{"list":{}},"promptCapabilities":{"image":false,"audio":false,"embeddedContext":true},"mcpCapabilities":{"http":false,"sse":false}},"agentInfo":{"name":"fp","title":"FP","version":"0.2.0"},"authMethods":[]}}
$ {"jsonrpc":"2.0","id":2,"result":{"sessionId":"s_261111_153012123456"}}
$ {"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s_261111_153012123456","update":{"sessionUpdate":"available_commands_update","availableCommands":[{"name":"help","description":"显示此帮助"}]}}}
{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"s_261111_153012123456","prompt":[{"type":"text","text":"列出当前目录的 .py 文件"}]}}
$ {"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s_261111_153012123456","update":{"sessionUpdate":"tool_call","toolCallId":"call_1","title":"🖥️ bash ls","kind":"execute","status":"in_progress","rawInput":{"cmd":"ls *.py"},"content":[{"type":"content","content":{"type":"text","text":"运行 ls"}}]}}}
$ {"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s_261111_153012123456","update":{"sessionUpdate":"agent_message_chunk","messageId":"msg_7f3a91c2b4d0","content":{"type":"text","text":"当前目录有 3 个 Python 文件：…"}}}}
$ {"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn"}}
```

即 `initialize` → `session/new` → `session/prompt`，内容全靠 `session/update` 反向下推。
错误响应形如 `{"jsonrpc":"2.0","id":9,"error":{"code":-32602,"message":"session/new 缺少必填参数 cwd（会话根目录绝对路径）"}}`。

## 文档

- [ACP 索引与本端合规基线](../acp/README.md)——能力协商、elicit/deferred 双路径、
  已实现/未实现清单（有 `tests/test_acp_wire.py` 守护）。
- 引擎与插件化：[引擎](../dev/引擎.md)、[工具系统](../dev/工具系统.md)、
  [会话模块](../dev/会话模块.md)、[命令系统](../dev/命令系统.md)、
  [中断机制](../dev/中断机制.md)。
- 规范原文：<https://agentclientprotocol.com/protocol/v1>

## 依赖

- Python ≥ 3.11
- [`fp-core >= 0.2.0`](https://pypi.org/project/fp-core/)（引擎、会话、工具与生命周期钩子）
- 伞包方式 `fp-agent[acp]` 会同时带入 `fp-core` 与适配层。

## License

MIT，见仓库根 [LICENSE](../../LICENSE)。
