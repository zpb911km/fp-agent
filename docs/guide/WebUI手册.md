# Five Pebbles WebUI 使用手册

> **版本**: 1.0.0  
> **文件**: `app/webui/main.py` + `app/webui/static/index.html`  
> **端口**: 8765（默认）

---

## 目录

1. [概述](#1-概述)
2. [快速启动](#2-快速启动)
3. [功能一览](#3-功能一览)
4. [架构说明](#4-架构说明)
5. [API 参考](#5-api-参考)
6. [WebSocket 协议](#6-websocket-协议)
7. [认证机制](#7-认证机制)
8. [会话管理](#8-会话管理)
9. [热重载机制](#9-热重载机制)
10. [开发指南](#10-开发指南)

---

## 1. 概述

WebUI 是 Five Pebbles 的**浏览器界面**，以插件模式运行在 Agent 核心之上，**不修改** `core/` 中的任何代码。

它提供了：

| 功能 | 说明 |
|------|------|
| 🖥️ **实时聊天** | 通过 WebSocket 流式通信，实时推送思考/工具调用/回复 |
| 🔐 **Token 认证** | 启动时自动生成唯一 Token，防止未授权访问 |
| 📋 **会话管理** | 查看历史会话、切换、删除、回溯到任意位置 |
| 🔄 **热重载** | 不重启服务器即可刷新 Agent 代码（开发利器） |
| 🆕 **新建 Agent** | 创建全新 Agent 实例（完全重置状态） |
| 🎨 **深色主题** | 赛博朋克风格 UI，响应式适配桌面/平板/手机 |
| 📐 **公式渲染** | 使用 KaTeX 渲染 LaTeX 数学公式 |
| 📝 **Markdown** | 使用 marked.js 渲染 Markdown 内容 |

---

## 2. 快速启动

### 2.1 环境要求

```bash
pip install 'fastapi[standard]' uvicorn
```

### 2.2 启动

```bash
cd /media/zpb/data/codes/AI/agent

# 方式一：模块启动（推荐）
python3 -m app.webui.main

# 方式二：直接启动
python3 app/webui/main.py
```

### 2.3 启动参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `8765` | 监听端口 |
| `--reload` | 关闭 | 启用 uvicorn 热重载（开发用，修改 `.py` 文件自动重启） |

### 2.4 访问

启动后终端会打印：

```
  🌐  WebUI: http://0.0.0.0:8765
  🔌  WS:    ws://0.0.0.0:8765/ws/chat
  📡  API:   http://0.0.0.0:8765/api/health
  🔑  启动 Token: xxxxxx
  📄  已写入: /media/zpb/data/codes/AI/agent/.webui_token
```

打开浏览器访问 `http://localhost:8765`，输入 Token 即可使用。

> **内网穿透**：如果在远程服务器上运行，可以用 `--host 0.0.0.0`（默认），通过 IP 访问。

---

## 3. 功能一览

### 3.1 聊天界面

- **发送消息**：在底部输入框输入，按 `Enter` 发送，`Shift+Enter` 换行
- **实时反馈**：Agent 思考时显示动画进度条，工具调用时显示可折叠卡片
- **回溯按钮**：每条消息右侧的 `↩ 回溯` 按钮，可回到该位置重新对话

### 3.2 顶栏按钮

| 按钮 | 功能 |
|------|------|
| 📋 **会话** | 打开历史会话列表浮层 |
| 🆕 **新建** | shutdown 旧 Agent，创建全新实例和会话 |
| 🔄 **重载** | 热重载所有核心模块，加载修改后的代码 |
| 🗑️ **清空** | 清空当前会话的消息记录 |

### 3.3 工具调用

当 Agent 调用工具时，前端以**可折叠卡片**形式展示：

- 🔧 显示工具名称和参数
- ⏳ 运行中状态
- ✅/❌ 完成状态
- 点击展开/折叠详情

### 3.4 历史会话

点击「📋 会话」打开浮层：

- 查看所有历史会话（摘要、消息数、创建时间）
- 切换会话（自动加载历史消息到界面）
- 删除会话（当前会话不可删除）
- 当前会话有蓝色边框标识

---

## 4. 架构说明

```
┌─────────────────────────────────────────────────┐
│                   浏览器（前端）                    │
│            HTML + CSS + Vanilla JS               │
└──────────────────────┬──────────────────────────┘
                       │ WebSocket (实时)
                       │ HTTP REST (查询)
                       ▼
┌─────────────────────────────────────────────────┐
│              FastAPI 服务器（后端）                │
│                                                  │
│  ┌──────────────┐    ┌──────────────────┐       │
│  │  REST API    │    │  WebSocket       │       │
│  │  /api/*      │    │  /ws/chat        │       │
│  └──────┬───────┘    └────────┬─────────┘       │
│         │                     │                  │
│         ▼                     ▼                  │
│  ┌──────────────────────────────────┐           │
│  │         EventBus                  │           │
│  │   异步发布/订阅事件总线           │           │
│  └──────────────┬───────────────────┘           │
│                 │ 生命周期钩子                    │
│                 ▼                                │
│  ┌──────────────────────────────────┐           │
│  │         WebUIPlugin              │           │
│  │   桥接插件：Agent → EventBus     │           │
│  └──────────────┬───────────────────┘           │
│                 │ 注册钩子                       │
│                 ▼                                │
│  ┌──────────────────────────────────┐           │
│  │         Agent 核心                │           │
│  │    LLM + Tools + Skills          │           │
│  └──────────────────────────────────┘           │
└─────────────────────────────────────────────────┘
```

### 核心组件

#### 4.1 EventBus（事件总线）

异步发布/订阅模式，一个 `asyncio.Queue` 的封装。

- 支持多个订阅者（多个浏览器标签页）
- 自动清理消费太慢的连接
- 最大缓冲 256 条事件

#### 4.2 WebUIPlugin（桥接插件）

监听 Agent 的**生命周期钩子**，将内部状态实时推送到前端：

| 钩子 | 事件 | 前端效果 |
|------|------|----------|
| `ON_BEFORE_LLM_CALL` | `llm_start` | 显示"思考中"动画 |
| `ON_AFTER_LLM_CALL` | `llm_end` | 显示 LLM 回复 |
| `ON_TOOL_SELECT` | `tool_select` | 显示计划调用的工具列表 |
| `ON_TOOL_CALL` | `tool_call` | 显示工具调用卡片 |
| `ON_TOOL_RESULT` | `tool_result` | 更新工具调用结果 |
| `ON_ERROR` | `error` | 显示错误信息 |
| `ON_BEFORE_RESPONSE` | `response` | 显示最终回复 |
| `ON_SHUTDOWN` | `shutdown` | 显示关闭通知 |

#### 4.3 认证中间件

FastAPI 的 `@app.middleware("http")`，拦截所有 `/api/*` 请求（白名单除外），验证 `Authorization: Bearer <token>` 头。

#### 4.4 WebSocket 端点

`/ws/chat` 端点使用 `token` 查询参数进行认证，连接后主循环：

1. 订阅 EventBus → 后台任务持续推送事件到前端
2. 接收前端消息 → 调用 `agent.process()` 处理
3. 处理过程中产生的所有事件通过 EventBus → WebSocket 实时推送

---

## 5. API 参考

所有 API 端点（除 `/api/auth`、`/api/health` 外）都需要 `Authorization: Bearer <token>` 头。

### 5.1 认证

#### `POST /api/auth`

验证 Token 并登录。

```json
// 请求
{ "token": "your_token_here" }

// 成功响应
{ "status": "ok", "message": "验证通过" }

// 失败响应
401 { "detail": "Token 无效" }
```

#### `GET /api/health`

健康检查。

```json
// 响应
{
  "status": "ok",
  "agent": "deepseek/deepseek-r1-0709",
  "session": "session_xxx",
  "subscribers": 1
}
```

### 5.2 聊天

#### `POST /api/chat`

发送消息并获取回复（非流式）。流式请使用 WebSocket。

```json
// 请求
{ "message": "你好" }

// 响应
{
  "response": "你好！我是 Five Pebbles...",
  "session_id": "session_xxx"
}
```

### 5.3 会话管理

#### `GET /api/sessions`

列出所有历史会话。

```json
// 响应
{
  "sessions": [
    {
      "id": "session_xxx",
      "message_count": 12,
      "created": "2026-06-07T20:00:00",
      "summary": "WebUI 文档讨论",
      "is_current": true
    }
  ]
}
```

#### `POST /api/sessions`

创建新会话并切换到它（自动为旧会话生成摘要）。

```json
// 响应
{ "session_id": "session_yyy", "status": "created" }
```

#### `DELETE /api/sessions/{session_id}`

删除指定会话（不能是当前会话）。

```json
// 响应
{ "status": "deleted", "session_id": "session_xxx" }
```

#### `GET /api/sessions/{session_id}`

获取指定会话的完整历史（按 agent 内部 context 格式返回）。

> **注意**：此端点会**修改 Agent 的当前状态**（切换会话再切回），频繁调用可能有副作用。如只需查看消息，请用下面 `/messages` 端点。

#### `GET /api/sessions/{session_id}/messages`

**推荐**。直接读文件获取消息，不触碰 Agent 状态。返回的消息按 1-based 索引排列，与 `/back` 命令编号一致。

```json
{
  "session_id": "session_xxx",
  "total": 12,
  "messages": [
    { "index": 1, "role": "user",     "content": "你好" },
    { "index": 2, "role": "assistant","content": "你好！" },
    { "index": 3, "role": "user",     "content": "帮我查个东西" },
    { "index": 4, "role": "assistant","content": "好的",
      "tool_calls": [{ "function": { "name": "web_search", "arguments": "..." } }] }
  ]
}
```

#### `POST /api/sessions/{session_id}/switch`

切换到指定会话。自动为旧会话生成摘要。

```json
// 响应
{ "session_id": "session_xxx", "status": "switched" }
```

#### `POST /api/sessions/clear`

清空当前会话内容。

```json
// 响应
{ "status": "cleared" }
```

### 5.4 Agent 控制

#### `POST /api/agent/new`

创建全新 Agent 实例。

**流程**：
1. shutdown 旧 Agent（保存会话后优雅退出）
2. 重新 `from core.agent import Agent` 获取最新类
3. 创建新 Agent，注册 WebUIPlugin
4. 创建全新会话（清空上下文）
5. 通过 EventBus 推送 `reload` / `reload_done` 事件通知前端

**安全保证**：
- 处理中请求 → 返回 `409 Conflict`
- 创建失败 → `_agent` 保持 `None`，下次请求自动重建
- 活跃 WebSocket 连接透明切换到新 Agent

```json
// 响应
{ "status": "ok", "session_id": "session_new", "model": "deepseek/..." }

// 冲突
409 { "detail": "Agent 正在处理请求，请稍后重试" }
```

#### `POST /api/reload`

热重载 Agent（不重启服务器）。

**流程**：
1. 保存当前会话上下文
2. shutdown 旧 Agent
3. `importlib.reload` 所有核心模块（按依赖顺序）
4. 创建新 Agent，注册 WebUIPlugin
5. 恢复旧会话
6. 通知 WebSocket 客户端重连

**热重载模块顺序**（按依赖）：

| 层级 | 模块 |
|------|------|
| 1 | `config`, `display` |
| 2 | `core.io`, `core.lifecycle`, `core.session`, `core.llm_client` |
| 3 | `plugins.base.plugin`, `prompts.agent`, `skills.loader` |
| 4 | `commands`, `tools`（含全局注册表重建） |
| 5 | `core.agent` |

```json
// 响应
{ "status": "ok", "session_id": "session_xxx", "model": "deepseek/..." }
```

---

## 6. WebSocket 协议

### 6.1 连接

```
ws://host:port/ws/chat?token=your_token
```

### 6.2 客户端 → 服务器

```json
// 发送消息
{ "type": "message", "content": "你好" }

// 心跳（服务器每 30 秒发 ping，客户端回复 pong）
{ "type": "ping" }
```

### 6.3 服务器 → 客户端

| type | 字段 | 触发时机 |
|------|------|----------|
| `connected` | `sub_id` | 连接成功 |
| `ping` | — | 心跳（30 秒超时保活） |
| `llm_start` | — | Agent 开始调用 LLM |
| `llm_end` | `content`, `has_tool_calls`, `tool_names` | LLM 返回结果 |
| `tool_select` | `tools: string[]` | Agent 选择要调用的工具 |
| `tool_call` | `name`, `args` | 工具开始执行 |
| `tool_result` | `name`, `result` | 工具执行完成 |
| `response` | `content` | Agent 生成最终回复 |
| `error` | `error` | 发生错误 |
| `done` | `session_id`, `final_content` | 一次请求处理完毕 |
| `cancelled` | — | 请求被取消 |
| `shutdown` | — | Agent 关闭 |
| `reload` | `message` | Agent 开始重载 |
| `reload_done` | `session_id`, `model` | Agent 重载完成 |
| `ask` | `prompt` | 交互式命令等待用户输入 |
| `info` | `content` | IO 通道信息 |
| `hint` | `content` | 提示信息 |
| `item` | `content` | 列表项输出 |

### 6.4 事件流示例

```
← { "type": "connected", "sub_id": "sub_0" }
→ { "type": "message", "content": "帮我搜一下今天的新闻" }
← { "type": "llm_start" }
← { "type": "tool_select", "tools": ["web_search"] }
← { "type": "llm_end", "has_tool_calls": true, "tool_names": ["web_search"] }
← { "type": "tool_call", "name": "web_search", "args": "新闻" }
← { "type": "tool_result", "name": "web_search", "result": "..." }
← { "type": "llm_start" }
← { "type": "llm_end", "content": "今天的新闻有以下几条：...", "has_tool_calls": false }
← { "type": "done", "session_id": "session_xxx", "final_content": "..." }
```

---

## 7. 认证机制

### 7.1 Token 生成

启动时自动在项目根目录生成 `.webui_token` 文件，内容为 `secrets.token_urlsafe(32)` 生成的随机字符串。

- **幂等设计**：文件已存在且内容有效（≥32 字符）则复用，不覆盖
- **终端显示**：启动时打印在终端，方便复制
- **路径**：`/media/zpb/data/codes/AI/agent/.webui_token`

### 7.2 验证流程

1. **前端**：打开页面 → 输入 Token → `POST /api/auth` 验证 → 存入 `sessionStorage`
2. **REST API**：`Authorization: Bearer <token>` 头，中间件拦截（`/api/auth` 和 `/api/health` 白名单）
3. **WebSocket**：`token` 查询参数，连接时验证

### 7.3 安全性

- 使用 `secrets.compare_digest()` 进行**常量时间比较**，防止时序攻击
- Token 存储在浏览器 `sessionStorage`（关闭标签页即失效）
- 部署时应确保 `.webui_token` 文件不被公开访问

---

## 8. 会话管理

### 8.1 会话生命周期

```
创建新会话 (POST /api/sessions)
    │
    ▼
发送消息 → 自动记录到当前会话
    │
    ▼
切换会话 (POST /api/sessions/{id}/switch)
    │  ├─ 保存旧会话上下文
    │  └─ 自动为旧会话生成摘要（调用 LLM）
    │
    ▼
删除会话 (DELETE /api/sessions/{id})
```

### 8.2 回溯机制

前端每条消息右侧的 `↩ 回溯` 按钮对应 Agent 的 `/back` 命令：

```
/back <消息索引> 2
```

点击后：
1. 清除当前界面
2. 通过 WebSocket 发送 `/back` 命令
3. Agent 回溯到该位置重新开始
4. `done` 事件到达后自动加载会话历史

### 8.3 摘要生成

切换/新建会话时，自动为旧会话生成摘要（5~10 个汉字的名字）：

1. **主方案**：调用 LLM，prompt 为 *"请总结一下，给这次对话起一个5到10个汉字的名字"*
2. **回退方案**：取首条用户消息的前 50 个字符
3. **最终回退**：`"empty_session"`

---

## 9. 热重载机制

### 9.1 适用场景

- **修改了核心代码**（`core/`、`prompts/`、`tools/` 等模块）
- **想在不重启 WebUI 服务器的前提下测试变更**
- 点击顶栏「🔄 重载」按钮或 `POST /api/reload`

### 9.2 原理

使用 Python 的 `importlib.reload()` 按依赖顺序逐层重新加载模块。

### 9.3 安全保证

| 场景 | 行为 |
|------|------|
| Agent 正在处理请求 | 返回 `409 Conflict`，拒绝重载 |
| 重载过程中模块报错 | `_agent` 保持 `None`，下次请求自动重建 |
| WebSocket 连接活跃 | 旧连接保有旧 `agent` 引用，可继续工作 |
| 重载完成后 | 自动恢复旧会话，WebSocket 透明切换到新 Agent |

### 9.4 前端行为

重载触发时，前端会：

1. 收到 `reload` 事件 → 显示「Agent 正在重载」
2. 收到 `reload_done` 事件 → 更新 session_id 和 model
3. 后台事件推送任务自动切换到新 EventBus 订阅

---

## 10. 开发指南

### 10.1 目录结构

```
app/webui/
├── __init__.py           # 空
├── main.py               # 后端服务器（FastAPI + WebSocket）
└── static/
    ├── index.html         # 前端页面（单页应用）
    ├── background.svg     # 背景图
    └── favicon.png        # 网站图标
```

### 10.2 启动开发模式

```bash
# 方式一：使用 uvicorn reload（修改 .py 文件自动重启）
python3 app/webui/main.py --reload

# 方式二：仅启动后端，前端用其他工具调试
python3 -c "
from app.webui.main import app
import uvicorn
uvicorn.run(app, host='0.0.0.0', port=8765, reload=True)
"
```

### 10.3 前端无构建

前端使用**纯 HTML + CSS + Vanilla JavaScript**，无构建工具、无框架依赖。

外部 CDN 依赖：
- `marked.js`（Markdown 渲染）
- `KaTeX`（公式渲染）

所有业务逻辑在 `index.html` 的 `<script>` 标签中。

### 10.4 调试

- **后端日志**：启动终端输出，包含 `[WebUI]` 前缀
- **前端日志**：浏览器控制台 `console.log` / `console.warn`
- **WebSocket 消息**：浏览器 Network → WS 面板可查看实时帧

### 10.5 添加新 API

1. 在 `main.py` 中添加新的 `@app` 路由
2. 如果不需要认证，添加到 `_AUTH_WHITELIST`
3. 在 `index.html` 的 JS 中通过 `authFetch()` 调用
4. 更新本文档

### 10.6 添加新事件类型

1. 在 `WebUIPlugin` 中添加新的生命周期钩子监听
2. 在 `handleEvent()` 中添加事件处理
3. 如需前端 UI 元素，添加对应的 DOM 渲染逻辑
4. 更新本文档的 WebSocket 协议表

---

## 附录

### A. 端口占用

```bash
# 查看 8765 端口是否被占用
lsof -i :8765

# 或
ss -tlnp | grep 8765
```

### B. 常见问题

**Q: 页面白屏 / 无法连接**
- 确认服务器已启动
- 检查防火墙/安全组是否放行 8765 端口
- 查看终端有无报错日志

**Q: Token 无效**
- 复制完整的 Token（含等号）
- 检查 `.webui_token` 文件内容
- 重启服务器生成新 Token

**Q: 重载后工具命令报错**
- 可能是因为 `importlib.reload` 未完全刷新所有子模块
- 尝试「🆕 新建」或完全重启服务器

### C. 相关文件

| 文件 | 用途 |
|------|------|
| `app/webui/main.py` | WebUI 后端服务器 |
| `app/webui/static/index.html` | 前端单页应用 |
| `.webui_token` | 自动生成的认证 Token |
| `core/agent.py` | Agent 核心类 |
| `core/lifecycle.py` | 生命周期钩子系统 |
| `core/io.py` | `WebSocketIO` 通道类 |

---

*文档版本: 1.0 · 最后更新: 2026-06-07*
