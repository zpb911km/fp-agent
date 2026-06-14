# fp-webui — 五块卵石 Web 界面

[![PyPI](https://img.shields.io/pypi/v/fp-webui)](https://pypi.org/project/fp-webui/)
[![Python](https://img.shields.io/pypi/pyversions/fp-webui)](https://pypi.org/project/fp-webui/)
[![License](https://img.shields.io/pypi/l/fp-webui)](LICENSE)

## 简介

**fp-webui** 是五块卵石（Five Pebbles）Agent 框架的 Web 图形界面。基于 FastAPI + Uvicorn 构建，提供浏览器端的 Agent 交互体验，支持实时流式对话、会话管理、配置调整等。

> 通过 `pip install fp[webui]` 或 `pip install fp-webui` 安装。

---

## 特性

- **浏览器端对话** — 现代化的 Web 聊天界面，无需安装终端
- **流式响应** — Server-Sent Events (SSE) 实时推送 LLM 输出
- **多会话管理** — 创建/切换/删除会话，历史记录永久保存
- **Markdown 渲染** — 代码语法高亮、表格、数学公式完美展示
- **深色/浅色主题** — 一键切换，护眼模式
- **配置面板** — 浏览器内调整 LLM 参数、切换模型
- **文件上传** — 支持拖拽上传文件作为上下文

---

## 安装

```bash
pip install fp-webui
```

要求 Python >= 3.11。

---

## 快速启动

### 命令行启动

```bash
fp-webui
```

或使用主包：

```bash
pip install fp[webui]
fp --webui
```

### 指定主机和端口

```bash
fp-webui --host 0.0.0.0 --port 8080
```

### 启动输出

```
╭─ Five Pebbles WebUI ─────────────────────╮
│                                           │
│  地址: http://localhost:7860              │
│  共享: http://192.168.1.100:7860          │
│                                           │
│  按 Ctrl+C 停止服务                       │
╰───────────────────────────────────────────╯
```

---

## 访问界面

打开浏览器访问 `http://localhost:7860`：

```
┌─────────────────────────────────────────┐
│  🤖 五块卵石                    [⚙] [🌙] │
├─────────────────────────────────────────┤
│                                         │
│  ┌─────────────────────────────────┐   │
│  │ 你好！我是五块卵石，有什么可以    │   │
│  │ 帮你的？                        │   │
│  └─────────────────────────────────┘   │
│                                         │
│  ┌─────────────────────────────────┐   │
│  │ 请用 Python 写一个 Web 服务器   │   │
│  │ [发送]                         │   │
│  └─────────────────────────────────┘   │
│                                         │
│  [会话: default] [新建] [历史]          │
└─────────────────────────────────────────┘
```

---

## API 接口（非 Web 页面场景）

fp-webui 同时暴露了 RESTful API，方便集成到其他前端或自动化工具中：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/chat` | POST | 发送消息并获取流式响应 (SSE) |
| `/api/sessions` | GET | 获取会话列表 |
| `/api/sessions` | POST | 创建新会话 |
| `/api/sessions/{id}` | DELETE | 删除会话 |
| `/api/config` | GET | 获取当前配置 |
| `/api/config` | PUT | 更新配置 |
| `/api/models` | GET | 获取可用模型列表 |
| `/health` | GET | 健康检查 |

### 调用示例

```bash
curl -N http://localhost:7860/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好", "session_id": "default", "stream": true}'
```

---

## 配置

通过命令行参数或环境变量配置：

```bash
# 命令行参数
fp-webui --host 127.0.0.1 --port 7860 --reload

# 环境变量
export FP_WEBUI_HOST=0.0.0.0
export FP_WEBUI_PORT=7860
export FP_WEBUI_THEME=dark
fp-webui
```

配置文件（YAML）：

```yaml
webui:
  host: "0.0.0.0"
  port: 7860
  theme: "dark"              # dark / light
  session_storage: "file"    # file / memory
  max_upload_size_mb: 10
  cors_origins: ["*"]
```

---

## 依赖

| 依赖 | 版本 | 用途 |
|------|------|------|
| `fp-core` | >= 2.0.0 | Agent 核心引擎 |
| `fastapi` | >= 0.122.0 | Web 框架 |
| `uvicorn` | >= 0.38.0 | ASGI 服务器 |

---

## 许可

MIT © zpb
