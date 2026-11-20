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
| `session/new` + `cwd` | `cwd` 为 required，登记为**会话属性**（会话根）；缺失/目录不存在 → `-32602`。**不 chdir**：相对路径基准由 `fp_core.core.cwd` 的 contextvar 按 Task 承载 |
| `session/list` | 请求四字段（`cwd`/`cursor`/`additionalDirectories`/`_meta`）**全可选**，发 `{}` 必须成功；`cwd` 按**每条会话自己的** cwd 过滤（realpath 归一，尾斜杠/软链等价）；键值游标分页（`_LIST_PAGE_SIZE=50`，末页无 `nextCursor`）；`updatedAt` 转 ISO 8601。老会话（meta 无 `cwd`）不带 `cwd` 时可见但 `cwd` 为空、`_meta.cwdUnknown=true` |
| 多会话并存 | 同一连接 `_sessions[sid]` 会话表；第二个 `session/new` **不顶掉**第一个；`session/prompt` 未知 sid → `-32602`；turn 内通知带**该会话自己**的 sid；`session/cancel` 只作用于目标会话 |
| 跨会话并发 | **排队**而非拒绝：`_turn_lock` 串行占用 core 执行槽（规范无「必须并发」的 MUST）。同会话并发仍是客户端 bug → `-32600` |
| `session/load` 历史回放 | 响应**之前**用 `user_message_chunk`/`agent_message_chunk` 回放全量历史；会话不存在 → `-32602`；已 `session/new` 但未落盘的内存态空会话可正常 load（回放为空） |
| `resource_link` | baseline MUST，转为「仅引用未附带内容」提示，不再静默丢弃 |
| `ToolKind` | 只发 schema 枚举内取值（`bash→execute`、未知→`other`） |
| `ToolCallContent` | 统一 `{"type":"content"/"diff"/"terminal"}` union 包装，`diff.path` 绝对化 |
| `rawInput` | 传 object（原始参数），不再传格式化字符串；不填 `rawOutput`（工具结果是字符串） |
| `toolCallId` | 按 fp-core 传入的真实 call id 配对，并行同名工具不错配 |
| `messageId` | 整轮共享一个 id（id 变化 = 新消息开始） |
| `plan` 生命周期 | `pending/in_progress/completed` 三态，结束时发空 `entries` 收尾 |
| 错误语义 | **同会话**并发 prompt → `-32600`（不用 `stopReason:"cancelled"`，那是 `session/cancel` 专属）；跨会话并发走排队不报错 |
| `session/close` | 释放本连接侧状态 + 该会话私有 MCP 子进程，**不删磁盘历史**（仍可 `session/load` 再开），幂等；`sessionCapabilities.close` 已声明 |
| 会话登记淘汰 | `_MAX_SESSIONS=64`：`session/new` 后顺手淘汰最久未用者，跳过当前聚焦与 `busy` 会话，淘汰即回收其私有 MCP（防长连接只增不减） |
| `mcpServers` 会话隔离 | 客户端经 `session/new`/`session/load` 转发的 server 为**会话私有**（`owner_sid`），可见性从执行期 contextvar 取 sid 过滤（与 cwd 同一范式）；`mcp_list`/`mcp_call` 按会话裁决，跨会话点名 → 拒绝。仅 stdio，`http`/`sse` 明确拒绝并记日志 |
| `compaction_update` | 门控于 `clientCapabilities.session.compaction`（**必须是对象**）；`in_progress` → `completed`/`failed`/`cancelled`，开态与终态**同 id**（每轮新 id，不复用）；不发 `compaction_summary_chunk` |
| 压缩判定（指纹） | 「是否真压缩」按**上下文指纹**（条数 + 内容哈希）判定，不靠文本也不只靠条数：`handled` 但内容未变 → `cancelled`（不伪造 `completed`）；条数相同而内容被改写 → `completed` |
| `session_info_update` | 标题（`meta.summary`）在一轮结束时**真变了**才推，`updatedAt` 带时区 ISO 8601；读标题走 `sessions.meta_of(sid)`（任意会话、只读、不 switch、不写盘） |
| `ToolCall.locations` 的 `line` | Follow-along 精确到行：`read_file` 用 `offset+1`，`edit_file` 用新内容落点，均 1-based。超大文件/无定位信息时**只发 `path` 不发 `line`**（宁可跳到文件头，也不编一个行号） |
| `locations` 补丁语义 | `tool_call_update` 里 `locations` **省略=保持不变**、`null`/`[]`=清空集合、非空=全量替换。按 schema 的字段补丁规则实现，不做「省略即清空」的猜测 |
| `agent_thought_chunk` | core 的 `thinking_content` 接进 ACP：独立 `messageId` + 独立缓冲（不与正文共用），`thinking_stop` 时冲刷。Zed 因此能显示推理过程（此前思考流在 ACP 侧被整段丢弃） |
| `configOptions`(model) + `session/set_config_option` | `session/new`/`load`/`resume` 响应均带 `configOptions`：唯一一项 `category=model`，`currentValue` 为 `provider/model`。切换走进程级单例并**广播** `config_option_update` 给所有已登记会话（模型是进程状态，不是会话状态——不广播会让其它 thread 显示过期值）。**不报 mode / thought_level**：fp 无此可调面，报了就是假能力 |
| `session/resume` | 只挂不重放（客户端手里已有历史），响应同样带 `configOptions`；能力位 `sessionCapabilities.resume` |
| `session/delete` | 删会话并连带删除磁盘历史（与 `session/close` 的区别）；能力位 `sessionCapabilities.delete`；未知 sid 幂等 |
| `usage_update` | 一轮结束时发一次：`used` = **最近一次** LLM 调用的 `prompt_tokens`（真实上下文长度，不是累计用量），`size` = `MAX_CONTEXT_TOKENS`（fp 自己的压缩阈值，比模型窗口小是常态）。一轮里多次工具循环只报最后一次；turn 外的隐性调用（摘要/标题/subagent）不计数 |
| `notice` | 门控于 `clientCapabilities.session.notices`（**必须是对象**才算出声明）；未声明时 MUST NOT 发，回落为一条 agent message（规范允许的等价表达） |

**未实现（已知缺口）**：

- `session/request_permission`：IDE 的「危险命令确认」UI 不生效。这是**章程决定**，不是缺口
  （见 `docs/architecture/no_dangerous_command_concept`）：fp 没有危险指令概念，不得引入执行前审批门。
- `logout`（`elicitation` 之外的认证面）：fp 本地无登录，`authMethods: []`。
- 客户端侧 `fs/*`、`terminal/*`：fp 用自己的工具，故 Zed 的文件树/集成终端看不到 fp 的动作
  （纯 UX 取舍：接进去能换来 IDE 侧可见性与权限弹窗，代价是放弃自己的终端管理）。
- **执行不并发**：跨会话是排队而非并行（`_turn_lock` 串行占用 core 唯一执行槽）。
  真并发（每会话一个 Agent / 或进程级隔离）未做；cwd 已改用 contextvar 按 Task 承载，
  是放开并发的前置条件，但不等于已支持并发。
- `session/set_mode`：未声明 `modes/availableModes`，故**不提供**伪实现（调用回 `-32601`）。

> 纪律：这些是「线上形状」硬约束，改动 `server.py` 的相关分支必须同步
> `tests/test_acp_wire.py`，否则接口漂移不可观测（历史上同类修复曾被一次重复的
> `def send` 静默抵消）。规范：https://agentclientprotocol.com/protocol/v1
>
> 关于 cwd：**不要**用 `os.chdir` 承载会话 cwd。那是进程级全局可变状态，正确性只能靠
> 「同一时刻只有一个会话在跑」这条纪律兜着，任何新代码（后台 job、subagent、插件里
> `await` 之后顺手 chdir）都会把正在跑的会话带到别的项目里，且没有异常/日志/测试会红。
> 会话路径一律走 `fp_core.core.cwd`（`current_cwd()` / `resolve_path()` / `set_cwd()`）。
>
> 绑定点是**唯一**的：`SessionManager._load_meta_from_session()` 装载会话时调
> `_bind_session_cwd()` 把基准切到 `meta.cwd`（`__init__`/`create_session`/`resume_latest`/
> `switch_session` 全走它），所以 CLI `/resume`、WebUI 切会话、ACP `session/load` 行为一致。
> 前端**无需**自己设基准；ACP 只在客户端声明的 cwd ≠ 落盘 meta 时做本轮覆盖。
> `cwd` 的持久化也因此提前到 `create_session`（`_ensure_file`）——只放内存的话切走即丢，
> 切回来只能按当前基准瞎猜，那正是「resume 旧会话落在启动目录」的根因。

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
