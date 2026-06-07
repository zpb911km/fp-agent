<div align="center">

# 🪨 Five Pebbles Agent

**基于生命周期钩子的插件化 Agent 框架**

![Python Version](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Version](https://img.shields.io/badge/version-2.0.0-orange)
![Code Size](https://img.shields.io/github/languages/code-size/zpb911km/fp-agent)
![Last Commit](https://img.shields.io/github/last-commit/zpb911km/fp-agent)


[🕹️ 快速开始](#-快速开始) •
[📖 文档](#-文档) •
[🏗️ 架构](#️-架构) •
[🔌 插件](#-插件) •
[📦 依赖](#-依赖) •
[🤝 贡献](#-贡献)

---

</div>

## ✨ 特性

- **🪝 生命周期驱动** — 23 个生命周期钩子覆盖 Agent 全流程，实现完全松耦合
- **🔌 插件即文件** — "文件即开关"设计：改文件名 == 改配置，零侵入启停插件
- **🛠️ 内置工具集** — bash 执行、文件读写、网页搜索/抓取、网络分析、逆向工程等 15+ 技能
- **🧩 命令系统** — `/help`, `/model`, `/session`, `/reset` 等 14 个内置命令
- **🔄 异步全栈** — `asyncio` + `httpx.AsyncClient`，全异步非阻塞 I/O
- **🧠 Subagent 派遣** — 创建独立子 agent 执行离线任务，不占用主对话上下文
- **🎨 富终端显示** — Rich + Markdown 渲染，彩色分类输出，自适应截断
- **🔐 零外部 SDK 依赖** — 自实现 LLM HTTP 客户端，不依赖 `openai` 等 SDK

## 🚀 快速开始

### 安装

```bash
# 1. 克隆仓库
git clone https://github.com/zpb/agent.git
cd agent

# 2. 创建虚拟环境（推荐）
python3 -m venv .venv
source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置 API Key
cp config.json config.json.example  # 保留模板
vim config.json                     # 填入你的 LLM_API_KEY
```

### 启动

```bash
# 直接启动 CLI
python cli.py

# 或使用入口模块
python -m agent
```

### 基本使用

```
$ python cli.py

  ╭──────────────────────────────────────────╮
  │  🪨  Five Pebbles Agent  v2.0.0          │
  │  输入 /help 查看命令列表                  │
  │  Ctrl+C 中断  |  Ctrl+D 保存退出          │
  ╰──────────────────────────────────────────╯

你 > 你好！

  🧠 思考中...     ← Spinner 动画
  ───────────────────────────────────────────
  你好！我是 Five Pebbles Agent，有什么可以帮你的？

你 > /help

  📋 可用命令:
    /help         显示帮助
    /model        切换模型
    /session      管理会话
    /reset        重置对话
    /compact      压缩对话
    /fork         分支对话
    /skills       管理技能
    /memory       管理记忆
    /exit         退出（保存会话）
    /exit!        强制退出（不保存）

你 > 运行 ls -la /
  🛠️  工具调用: bash("ls -la /")
  📦 输出: ... (工具结果自动展示)
```

### 作为库使用

```python
import asyncio
from agent import Agent

async def main():
    agent = Agent()
    
    # 处理消息（自动调用 LLM + 工具循环）
    response = await agent.process("帮我搜索 Python 异步编程教程")
    print(response.content)
    
    await agent.shutdown()

asyncio.run(main())
```

## 🏗️ 架构

```
agent.py (入口/导出)
│
├── cli.py                    ─ 交互式 CLI (prompt_toolkit)
│
├── core/                     ─ 核心引擎
│   ├── agent.py              ─ Agent 主干（流程派发）
│   ├── lifecycle.py          ─ 生命周期管理器 (23 钩子)
│   ├── llm_client.py         ─ LLM HTTP 客户端（自实现）
│   └── session.py            ─ 会话持久化管理
│
├── plugins/                  ─ 插件系统
│   ├── base/plugin.py        ─ 插件基类 + PluginRegistry
│   └── notification.py       ─ 桌面通知/声音提醒
│
├── tools/                    ─ 工具执行引擎
│   ├── core.py               ─ 核心工具（bash/文件/搜索等）
│   ├── __init__.py           ─ ToolRegistry（分发+调度）
│   └── plugins/              ─ 工具插件（web_search, web_fetch）
│
├── commands/                 ─ CLI 命令（/help, /model 等）
│
├── skills/                   ─ 技能系统（技能即 .md 文件）
│   ├── loader.py             ─ 技能加载器
│   └── *.md                  ─ 15+ 预置技能描述
│
├── prompts/                  ─ 系统提示词模板
│
├── config.py                 ─ 配置管理（三级优先级）
├── config.json               ─ 实际配置值
├── display.py                ─ 终端显示模块
├── memory.py                 ─ 记忆持久化
│
├── agent.py                  ─ 入口（导出核心类）
├── cli.py                    ─ CLI 入口
│
├── docs/                     ─ 文档
│   ├── 用户手册.md
│   └── 开发手册.md
│
├── LICENSE                   ─ MIT 许可证
├── pyproject.toml            ─ 项目元数据
├── CHANGELOG.md              ─ 更新日志
└── CONTRIBUTING.md           ─ 贡献指南
```

### 数据流

```
用户输入
    │
    ▼
┌─────────────────────────────┐
│  CLI (cli.py)               │
│  ├── 解析命令 / 普通消息     │
│  └── 调用 Agent.process()   │
└─────────────┬───────────────┘
              │
              ▼
┌─────────────────────────────┐
│  Agent 主干 (core/agent.py) │
│  ├── ON_MESSAGE_RECEIVED    │ ← 插件拦截
│  ├── 调用 LLM               │
│  ├── ON_TOOL_CALL           │ ← 解析工具调用
│  └── 循环直到无工具调用     │
└─────────────┬───────────────┘
              │
              ▼
┌─────────────────────────────┐
│  工具执行 (tools/)           │
│  ├── web_search / web_fetch │
│  ├── bash 命令执行          │
│  ├── 文件读写               │
│  └── ...                    │
└─────────────────────────────┘
```

## 🔌 插件系统

### 文件即开关

插件启停不需要改配置、不碰代码——**改文件名就够了**：

```
plugins/
├── notification.py         # ✅ 启用
├── notification.py.disabled   # ❌ 停用（加后缀 .disabled）
├── old_plugin.py.v2           # ❌ 停用（加任意后缀）
├── __pycache__/               # ⚠️ 自动忽略
└── base/                      # ⚠️ 插件基类目录
```

### 编写自定义插件

```python
from agent import Plugin, LifecycleHook

class WeatherPlugin(Plugin):
    name = "weather"
    version = "1.0.0"

    def on_register(self, lifecycle):
        lifecycle.register(
            LifecycleHook.ON_MESSAGE_RECEIVED,
            self.on_message,
            priority=50,
        )

    async def on_message(self, ctx, **kwargs):
        content = kwargs.get("content", "")
        if "天气" in content:
            print("检测到天气查询请求！")
        return ctx
```

### 内置生命周期钩子

| 阶段 | 钩子 | 触发时机 |
|------|------|---------|
| **初始化** | `ON_INIT`, `ON_CONFIG_LOADED` | Agent 启动时 |
| **消息处理** | `ON_MESSAGE_RECEIVED`, `ON_MESSAGE_PARSE`, `ON_MESSAGE_FILTER` | 收到用户输入 |
| **思考** | `ON_BEFORE_THINK`, `ON_THINK`, `ON_AFTER_THINK` | Agent 处理中 |
| **LLM 交互** | `ON_BEFORE_LLM_CALL`, `ON_LLM_CALL`, `ON_AFTER_LLM_CALL` | 调用大模型 |
| **响应** | `ON_BEFORE_RESPONSE`, `ON_RESPONSE`, `ON_AFTER_RESPONSE` | 生成回复 |
| **工具执行** | `ON_TOOL_SELECT`, `ON_TOOL_CALL`, `ON_TOOL_RESULT`, `ON_TOOL_ERROR` | 使用工具 |
| **资源管理** | `ON_SHUTDOWN`, `ON_CLEANUP` | Agent 关闭 |

## 📦 依赖

项目非常轻量，仅依赖 **6 个第三方包**：

| 包名 | 用途 | 版本要求 |
|------|------|---------|
| `httpx` | 异步 HTTP 客户端（调用 LLM API + 网页抓取） | ≥0.28.0 |
| `prompt_toolkit` | 交互式 CLI（自动补全、多行输入） | ≥3.0.40 |
| `rich` | 终端富文本显示 | ≥14.0.0 |
| `wcwidth` | 中英文混排字符宽度计算 | ≥0.2.0 |
| `PyYAML` | 技能 YAML 配置解析 | ≥6.0 |
| `httpx` | HTTP 客户端（搜索后端用） | ≥0.28.0 |

## 🧰 内置技能

| 技能 | 描述 |
|------|------|
| `bash` | 执行 shell 命令 |
| `read_file` / `write_file` / `edit_file` | 文件读写操作 |
| `web_search` / `web_fetch` | 互联网搜索与网页抓取 |
| `file_fingerprint` | 文件类型识别（file + strings） |
| `elf_analysis` | ELF 可执行文件逆向分析 |
| `python_syntax_check` | Python 语法检查 |
| `process_tracking` | 进程行为动态追踪 |
| `privilege_escalation_scan` | 权限提升线索扫描 |
| `env_pollution_check` | 环境变量污染检测 |
| `self_modification` | 源代码自我修改与验证 |
| `yt_dlp_download` | 视频下载 |
| ... | [完整列表 →](docs/用户手册.md) |

## 📚 文档

- [📖 用户手册](docs/用户手册.md) — 安装、配置、CLI 命令、技能使用
- [🔧 开发手册](docs/开发手册.md) — 架构设计、API 参考、插件/技能/工具开发指南
- [📋 更新日志](CHANGELOG.md) — 版本历史
- [🤝 贡献指南](CONTRIBUTING.md) — PR 流程、代码规范

## 🧪 测试

```bash
# 语法检查
python3 -c "import ast; ast.parse(open('core/agent.py').read()); print('OK')"

# 中断测试
python3 test_interrupt.py

# 快速功能验证
echo "你好" | python3 cli.py
echo "运行 ls -la" | python3 cli.py
```

## 🤝 贡献

欢迎贡献！详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

简单来说：

1. Fork 仓库
2. 创建功能分支 (`git checkout -b feat/xxx`)
3. 提交代码（遵循 [Conventional Commits](https://www.conventionalcommits.org/)）
4. 创建 Pull Request

## 📄 许可证

[MIT License](LICENSE) © 2024-2026 zpb

---

<div align="center">
  <sub>Built with 🪨 by Five Pebbles</sub>
</div>
