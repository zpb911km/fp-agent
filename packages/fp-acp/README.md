# fp-acp — 五块卵石 ACP 通信协议

[![PyPI](https://img.shields.io/pypi/v/fp-acp)](https://pypi.org/project/fp-acp/)
[![Python](https://img.shields.io/pypi/pyversions/fp-acp)](https://pypi.org/project/fp-acp/)
[![License](https://img.shields.io/pypi/l/fp-acp)](LICENSE)

## 简介

**fp-acp** 实现了五块卵石（Five Pebbles）的 Agent Communication Protocol（ACP），一个基于 **JSON-RPC 2.0** 的通信协议。它允许外部进程（IDE 插件、其他 Agent、自动化脚本）通过标准化的 API 与 Agent 交互。

> 通过 `pip install fp[acp]` 或 `pip install fp-acp` 安装。

---

## 什么是 ACP？

ACP（Agent Communication Protocol）定义了 Agent 与外部世界之间的标准通信契约。它使得：

- **IDE 集成** — VS Code、Neovim、Emacs 等编辑器通过 ACP 调用 Agent 能力
- **跨 Agent 通信** — 多个 Agent 实例通过 ACP 协作完成任务
- **自动化管道** — CI/CD 脚本通过 ACP 集成 Agent 进行代码审查、文档生成等

```
┌──────────┐    JSON-RPC 2.0    ┌───────────┐
│   IDE    │ ◄────────────────► │  ACP      │
│ 插件/    │                    │  Server   │──► fp-core Agent
│  脚本    │                    │  (fp-acp) │
└──────────┘                    └───────────┘
```

---

## 安装

```bash
pip install fp-acp
```

要求 Python >= 3.11。

---

## 快速使用

### 默认端口

```bash
fp-acp
```

### 指定主机和端口

```bash
fp-acp --host 127.0.0.1 --port 9090
```

启动日志：

```
╭─ Five Pebbles ACP Server ────────────────╮
│                                           │
│  监听地址: tcp://127.0.0.1:9090          │
│  协议: JSON-RPC 2.0                      │
│                                           │
│  按 Ctrl+C 停止服务                       │
╰───────────────────────────────────────────╯
```

---

## JSON-RPC 接口

ACP 使用标准的 JSON-RPC 2.0 协议，支持以下方法：

### 核心方法

| 方法 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `chat` | `{message, session_id?, stream?}` | `{response}` | 发送消息并获取回复 |
| `chat_stream` | `{message, session_id?}` | SSE 流 | 流式聊天（Server-Sent Events） |
| `tools/list` | `{}` | `[{name, description, parameters}]` | 获取可用工具列表 |
| `tools/call` | `{name, arguments}` | `{result}` | 直接调用工具 |
| `sessions/list` | `{}` | `[{id, created_at, message_count}]` | 列出会话 |
| `sessions/create` | `{id?}` | `{id}` | 创建新会话 |
| `sessions/delete` | `{id}` | `{success}` | 删除会话 |
| `sessions/history` | `{id, limit?}` | `[{role, content}]` | 获取会话历史 |
| `config/get` | `{}` | `{config}` | 获取当前配置 |
| `config/set` | `{key, value}` | `{success}` | 更新配置项 |
| `ping` | `{}` | `{pong}` | 健康检查 |
| `shutdown` | `{}` | `{success}` | 优雅关闭 Server |

### 调用示例

#### 使用 curl

```bash
# 发送聊天消息
curl -X POST http://127.0.0.1:9090 \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "chat",
    "params": {
      "message": "你好，请介绍一下自己",
      "session_id": "my-session"
    }
  }'
```

响应：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "response": "你好！我是五块卵石，一个基于生命周期钩子的插件化 Agent。"
  }
}
```

#### 使用 Python

```python
import httpx

rpc_request = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list",
    "params": {}
}

response = httpx.post("http://127.0.0.1:9090", json=rpc_request)
tools = response.json()["result"]
print(tools)
```

---

## 使用场景

### 1. VS Code 集成

通过 ACP，VS Code 扩展可以直接调用 Agent 进行代码审查、重构建议、自动补全：

```
用户选中代码 → 右键 "Ask Five Pebbles" → ACP Server 返回分析结果
```

### 2. 多 Agent 协作

```python
# Agent A 通过 ACP 调用 Agent B 的专业能力
response = httpx.post("http://agent-b:9090", json={
    "jsonrpc": "2.0",
    "method": "chat",
    "params": {"message": "请分析这段代码的安全性: ..."}
})
```

### 3. CI/CD 集成

```yaml
# .github/workflows/code-review.yml
jobs:
  review:
    steps:
      - run: |
          curl -X POST http://agent:9090 \
            -H "Content-Type: application/json" \
            -d '{
              "method": "chat",
              "params": {
                "message": "Review the diff in commit ${{ github.sha }}"
              }
            }'
```

---

## 安全说明

- ACP Server 默认绑定 `127.0.0.1`（仅本地访问）
- 如果需要远程访问，使用 `--host 0.0.0.0` 并配合防火墙或反向代理
- 生产环境建议添加 TLS 加密和 API 认证

---

## 依赖

| 依赖 | 版本 | 用途 |
|------|------|------|
| `fp-core` | >= 0.1.0 | Agent 核心引擎 |

---

## 许可

MIT © zpb
