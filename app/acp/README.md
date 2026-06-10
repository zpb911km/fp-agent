# ACP Server — 让五块卵石进入你的 IDE

## 这是什么

**ACP (Agent Client Protocol)** 是连接 IDE 和 AI Agent 的标准协议，类比 LSP（语言服务器协议）但面向 AI 编码助手。

这个模块让 Five Pebbles 作为 **ACP Server** 运行，使你可以在 VS Code / Zed / JetBrains 等编辑器中直接使用五块卵石的全部能力——选中代码、提问、改文件，全程无需离开编辑器。

## 协议架构

```
VS Code (ACP Client)
    │  stdio (JSON-RPC 2.0, 一行一条 JSON)
    ▼
Five Pebbles (ACP Server) ← 本模块
    │
    ▼
Agent.process() ← 五块卵石核心引擎
    │
    ├── bash / read_file / write_file / edit_file
    ├── subagent / vision / web_search
    ├── file_fingerprint / elf_analysis / ...
    └── 所有 15+ 工具
```

## 快速开始

### 1. 启动 ACP Server

```bash
cd /path/to/agent
python3 -m app.acp
```

什么都不会输出（日志走 stderr，stdout 是 JSON-RPC 通道）。

### 2. 配置 VS Code

安装 [ACP Client](vscode:extension/formulahendry.acp-client) 扩展。

在 VS Code 设置中（`.vscode/settings.json` 或用户设置）添加：

```json
{
  "acp-client.servers": [
    {
      "name": "Five Pebbles",
      "command": "python3",
      "args": ["-m", "app.acp"],
      "cwd": "/path/to/agent"
    }
  ]
}
```

重启 VS Code 后，在命令面板中运行 `ACP: Start Server`，选择 "Five Pebbles"。

### 3. 在 Zed 中配置

在 Zed 的 `settings.json` 中添加：

```json
{
  "acp_servers": {
    "Five Pebbles": {
      "command": "python3",
      "args": ["-m", "app.acp"],
      "cwd": "/path/to/agent"
    }
  }
}
```

## 使用方法

连接成功后，你可以在 IDE 中：

1. **选中一段代码**
2. **打开 ACP 对话面板**（快捷键通常为 `Ctrl+Shift+I` 或 `Cmd+Shift+I`）
3. **输入你的问题**——例如"重构这个函数"、"解释这段代码"、"添加类型注解"
4. 五块卵石会**感知当前上下文**（文件路径、选中内容），给出精准回答

### 斜杠命令

在 ACP 模式下，内置的斜杠命令（如 `/session`、`/help`）会直接在 Agent 层执行，
结果通过聊天面板返回：
- `/session` — 查看当前会话 ID
- `/help` — 列出所有可用命令
- `/history` — 查看当前会话历史
- `/clear` — 清空当前会话
- 其他未知的 `/` 命令会被自动转义为纯文本，不会报错

### Agent 的能力

连接后，五块卵石的**全部工具**都可用：

| 工具 | 作用 |
|------|------|
| `bash` | 执行 Shell 命令 |
| `read_file` / `write_file` / `edit_file` | 直接操作文件 |
| `subagent` | 派遣子 Agent 并行处理 |
| `web_search` / `smart_web_search` | 联网搜索 |
| `vision` | 图像识别 |
| `file_fingerprint` / `elf_analysis` | 文件逆向分析 |
| `python` | 执行 Python 代码 |
| ... | 15+ 工具全部可用 |

## 协议细节

### 通信方式

- **stdin**: JSON-RPC 2.0 请求（IDE → Agent）
- **stdout**: JSON-RPC 2.0 响应 + 通知（Agent → IDE）
- **stderr**: 日志（不受协议约束，IDE 通常显示在调试控制台）

每条消息为**一行 JSON**（newline-delimited JSON），与 LSP 相同。

### 支持的方法

| 方法 | 方向 | 说明 |
|------|------|------|
| `initialize` | IDE → Agent | 协议握手 |
| `initialized` | IDE → Agent | 通知：IDE 已就绪 |
| `session/new` | IDE → Agent | 创建新会话 |
| `session/load` | IDE → Agent | 恢复已有会话 |
| `session/prompt` | IDE → Agent | 发送用户消息（核心） |
| `session/set_mode` | IDE → Agent | 切换模式（预留） |
| `session/cancel` | IDE → Agent | 取消当前操作（通知） |
| `session/update` | Agent → IDE | 推送进度/工具调用（通知） |

### 示例交互

```json
// IDE → Agent: 初始化
→ {"jsonrpc":"2.0","id":1,"method":"initialize",
   "params":{"clientInfo":{"name":"VS Code","version":"1.96.0"}}}

// Agent → IDE: 握手完成
← {"jsonrpc":"2.0","id":1,"result":{
     "protocolVersion":"2026-01-21",
     "capabilities":{"streaming":false},
     "serverInfo":{"name":"Five Pebbles","version":"2.0.0"}}}

// IDE → Agent: 提问（含编辑器上下文）
→ {"jsonrpc":"2.0","id":2,"method":"session/prompt",
   "params":{
     "messages":[{"role":"user","content":"这个函数有什么问题？"}],
     "context":{"file_path":"/project/src/main.py","selection":"def foo(): pass"}
   }}

// Agent → IDE: 进度通知
← {"jsonrpc":"2.0","method":"session/update",
   "params":{"session_id":"...","type":"plan",
     "content":{"steps":[{"title":"思考中...","status":"running"}]}}}

// Agent → IDE: 回复
← {"jsonrpc":"2.0","id":2,"result":{
     "messages":[{"role":"assistant","content":"这个函数..."}]}}
```

## 当前限制

| 限制 | 说明 | 计划 |
|------|------|------|
| **流式响应** | v1 暂不流式输出，等完整处理完才返回 | v2 可添加 |
| **取消操作** | `session/cancel` 收到后需等当前 LLM 调用完成 | v2 改进 |
| **文件感知** | IDE 中未保存的编辑内容暂不可见（Agent 读的是磁盘文件） | v2 支持 `fs/read_text_file` |
| **多会话** | 一个 ACP Server 实例只服务一个 IDE 连接 | v2 支持多路复用 |

## 架构说明

```
stdout 保护机制：
  Agent 内部有很多 print/display 输出（spinner、工具调用信息、shutdown panel）。
  如果这些输出跑到 stdout，会破坏 JSON-RPC 通道。

  解决方案：
  - Agent.__init__ 被 redirect_stdout(sys.stderr) 包裹
  - Agent.process() 在 _dispatch 中被 redirect_stdout(sys.stderr) 包裹
  - JSON-RPC 消息使用 self._stdout.write() 直接写入真正的 stdout
  - 日志走 stderr
```
