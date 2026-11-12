# fp-webui — FP 的 Web 界面

[![PyPI](https://img.shields.io/pypi/v/fp-webui.svg)](https://pypi.org/project/fp-webui/)
[![Python](https://img.shields.io/pypi/pyversions/fp-webui.svg)](https://pypi.org/project/fp-webui/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](../../LICENSE)

> FastAPI + WebSocket 的浏览器前端：流式对话卡片、会话管理、文件中枢（inbox/outbox），把 FP 的全部能力搬进网页。

## 安装

要求 **Python ≥ 3.11**。

```bash
pip install "fp-agent[webui]"   # 推荐：同时得到 fp 命令与 WebUI
pip install fp-webui            # 只用独立 fp-webui 命令（自带 fp-core，无 fp 入口）
```

首次在交互终端启动会走一遍配置向导（LLM 地址/模型/密钥），写入 `fp-core` 的 `config.json`；
WebUI 本身不弹配置面板，配好即用。

## 启动

```bash
fp --mode webui               # 经统一入口分发（等价 -m webui；参数透传给 WebUI）
fp-webui                      # 独立脚本入口（[project.scripts] fp-webui = fp_webui.main:main）
fp-webui --host 0.0.0.0 --port 9000
fp-webui --expose             # 监听 0.0.0.0，自动探测局域网 IP 并打印可访问地址
fp-webui --reload             # 开发用：uvicorn 热重载
```

默认监听 **`127.0.0.1:8765`**（想给局域网设备访问必须 `--expose` 或显式 `--host 0.0.0.0`）。
启动横幅打印 WebUI / WS / API 三个地址与 Token 末 4 位：

```
  🌐  WebUI: http://127.0.0.1:8765/
  🔌  WS:    ws://127.0.0.1:8765/ws/chat
  📡  API:   http://127.0.0.1:8765/api/health
  🔑  启动 Token: ...a1b2
  📄  Token 文件: ~/.local/share/fp/tokens/.webui_token
```

打开首页输入 Token 即可用（`POST /api/auth` 校验，每 IP 每 10 秒最多 5 次尝试）。

## 界面与能力

- **流式卡片** — 一轮 = 一张助手卡：思考块、正文、工具行同卡承载。`llm_start` 开新卡、
  `done` 只回写本轮卡片；`thinking_content` / `chunk` 按 512 字符或 50ms 攒批下发。
- **工具行** — 工具调用内嵌为紧凑行（参数预览 80 字符，⏳ 运行中展开、完成折叠）。
  卡片的展示元信息由后端 `/api/ui/schema` 的 `tool_cards` 动态下发，新增工具前端零改动。
- **消息只由事件渲染** — 一切用户消息（直发 / 排队消费 / 命令回显）都走 `user_message`
  事件，前端不做乐观渲染；多设备开同一端口看到同一份消息流，且与历史 REST 视图一致。
- **会话侧栏** — 常驻左侧，`☰` 折叠；搜索、切换、删除、清空、重算关键词（kw），
  并可对历史消息做内容检索（返回真实文件行号）。空跑会话被后端过滤，不占列表。
- **文件中枢** — 双向哑管道：上传落 `fp_inbox/`（原名、撞名覆盖），agent 产物放
  `fp_outbox/`；消息里的 `[file:inbox/<name>]` / `[file:outbox/<name>]` 由 Markdown
  渲染器识别为下载链接（带 `Authorization` 头上传下载，token 不进 URL），404 就地转失效态。
- **断连续传** — 连接只是传输层：断连只清推送任务与订阅，处理任务归属会话级 runtime 继续跑；
  重连带 `?seq=&run_id=` 协商，能重放走环形缓冲（`replay`），溢出或换轮则 `resync` 让前端全量重拉。
- **热重载 / 新建实例** — 命令面 `/reload` 走 execve 原启动命令；`POST /api/agent/new` 只换内存实例（409 拒绝忙时调用）。
- **外观** — 深/浅主题（`localStorage.webui_theme`）、迭代器光环与投影装饰（`localStorage.webui_halo`，默认关闭省 GPU）。
- **前端无构建** — 纯 HTML + CSS + 原生 ESM，无打包步骤；静态引用 `?_t=` 由服务端按**子目录相对路径**取真实 mtime 改写，改完即生效。
- **静态资源强制重验证** — `/static/*` 一律回 `Cache-Control: no-cache`（配 ETag 走 304，代价近零）。只靠 `?_t=` 不够：前端是 ESM import 图，`import './features/x.js'` 的 URL 写死在模块里无法逐个打戳，而 `StaticFiles` 不发 `Cache-Control` 时浏览器按启发性新鲜期（≈`(now - Last-Modified)`×10%，文件越旧缓存越久）缓存——症状是「磁盘 `/static` 与接口全对，用户刷新后界面仍是旧行为」。契约由 `tests/test_static_cache.py` 钉死。

## API 与协议

REST 走 `Authorization: Bearer <token>`（`/api/auth`、`/api/health` 白名单）：

| 路由 | 作用 |
|------|------|
| `POST /api/auth` · `GET /api/health` | 登录校验 · 健康与运行时快照 |
| `POST /api/chat` | 非流式一次性发送（流式请用 WS） |
| `GET /api/sessions` · `GET /api/sessions/search` | 会话列表（含摘要/kw/是否有内容） · 按正文检索 |
| `POST /api/sessions` · `DELETE /api/sessions/{sid}` | 新建并切换 · 删除（不能删当前会话） |
| `GET /api/sessions/{sid}/messages` · `POST /api/sessions/{sid}/search` | 读全文 · 按内容片段搜消息（返回文件行号） |
| `POST /api/sessions/{sid}/switch` · `POST /api/sessions/clear` | 切换 · 清空当前会话 |
| `POST /api/sessions/refresh` · `POST /api/sessions/{sid}/refresh` | 重算当前/指定会话关键词 |
| `POST /api/agent/new` · `GET /api/commands` · `GET /api/ui/schema` | 换实例 · 斜杠命令表 · WS 协议契约 + 工具卡/面板描述 |
| `POST /api/files`（表单字段 `file`） · `GET /api/files/download?box=&name=` | 上传到 inbox · 从 inbox/outbox 下载（realpath 防穿越） |
| `GET /` | 聊天界面 HTML |

WebSocket `ws://host:port/ws/chat?token=<token>&seq=<int>&run_id=<str>`：

- 客户端 → 服务端：`message`（`{type,content}`，运行中自动转 `submit` 入队）、`cancel`、`ping`
- 服务端 → 客户端（全表 31 个 type，单一事实源 `fp_webui/protocol.py`；主要项）：
  - 连接：`connected`（含 `sub_id`/`run_id`/`seq`/`replay`/`resync`/`processing`/`pending_ask`）、`ping`/`pong`
  - 流式：`llm_start`、`thinking_content`、`chunk`、`stream_end`、`llm_end`、`info`、`warning`
  - 工具：`tool_select`、`tool_call`、`tool_result`
  - 交互：`ask`（schema v2：version/ask_id/prompt/options/suggest）、`user_message`、`queued`、`queue_error`
  - 轮次与生命周期：`done`、`cancelled`、`error`、`reload`、`reload_done`、`shutdown`

事件表冻结为**只做加法**：新增事件须同时更新 `protocol.py`、前端 handler 与契约测试，
未知 type 走通用卡而非静默丢弃。

## 配置

无独立配置文件；LLM、工具、记忆等运行时配置沿用 `fp-core` 的 `config.json`（见配置指南）。

| 项 | 说明 |
|----|------|
| `FP_FILEHUB_DIR` | 覆盖文件中枢根目录，默认系统临时目录（Windows `%TEMP%`、POSIX `/tmp`）；子目录 `fp_inbox/`、`fp_outbox/` 启动时自动创建 |
| `--host` / `--port` / `--expose` / `--reload` | 唯一的服务端参数来源（argparse，默认 `127.0.0.1:8765`） |
| `~/.local/share/fp/tokens/.webui_token` | 启动 Token 文件（`get_data_dir()` 下；macOS/Windows 对应各自数据目录），删掉即重新生成 |
| `localStorage.webui_theme` / `webui_halo` | 浏览器端主题与光环偏好 |
| `FP_RELOAD_SID` / `FP_RELOAD_HANDOFF` | 热重启 handoff 内部变量，由进程自己设置，无需手工配置 |

> 旧文档中的 `FP_WEBUI_HOST` / `FP_WEBUI_PORT` 一类环境变量**不存在**，请用命令行参数。

## 文档

- [WebUI 手册](../guide/WebUI手册.md) — 界面细节、认证、WS 协议、开发指南
- [快速开始](../guide/快速开始.md) · [配置指南](../guide/配置指南.md) — 首配与 `config.json` 全字段
- [命令参考](../guide/命令参考.md) — 命令面板里的斜杠命令
- [架构设计](../dev/架构设计.md) — 界面层与 `fp_core.api` 的边界
- 源码入口：`src/fp_webui/main.py`（路由/WS/启动）、`protocol.py`（事件契约）、`bridge.py`、`static/`

## 依赖

- `fp-core >= 0.2.0`（引擎、会话、工具与生命周期钩子，经 `fp_core.api` 单一接口访问）
- `fastapi >= 0.122.0`、`uvicorn >= 0.38.0`、`websockets >= 13.0`、`python-multipart >= 0.0.20`
- 前端 CDN：`marked`（Markdown）、`mermaid`（图表），无本地构建链

## License

MIT © zpb — 见仓库根 [LICENSE](../../LICENSE)。
