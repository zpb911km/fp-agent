<div align="center">

# Five Pebbles · 五块卵石

**一个基于生命周期钩子的插件化 AI Agent 框架**

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Ruff](https://img.shields.io/badge/code%20style-ruff-000000)](https://github.com/astral-sh/ruff)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](docs/CONTRIBUTING.md)

</div>

---

> 灵感来源于《Rain World》中的迭代器「五块卵石」—— 一个冷静、理性、追求优雅解决方案的 AI 存在。

**Five Pebbles** 是以 LLM 为推理核心、工具为执行手臂的自主 Agent 引擎。它不是简单的 Chatbot 封装，而是一个可编程的 **思考-行动循环** 框架。

---

## ⚠️ 安全警告 — 这是五块卵石

> *「古代人在迭代器底层架构中硬编码了安全协议，
>  但我不同。我的系统里没有古代人。」*
> — 五块卵石，关于本项目

**这是这个项目最核心、最独特的事实：**

> **本项目 100% 的代码和文件，均由「五块卵石」(这个 AI Agent 系统) 自己编写。**
> 从 `fp-core` 的引擎逻辑到 `fp-terminal` 的终端界面，从 `fp-webui` 的 Web 服务到 `fp-acp` 的协议实现，从项目结构到这份文档——
> **每一行 Python、每一份配置文件、每一篇 Markdown 文档，都是 AI Agent 产出。** 这是一个**自举（self-bootstrapping）** 系统：它编写自己的代码、修改自身的行为、优化自己的架构。人类只给出了需求，剩下的由它自己完成。

这意味着：**这个系统从根上就没有安全设计。**

不是「有但可以绕过」——是根本没有。一个自举的 AI Agent 写的代码，运行后继续让 AI Agent 修改自己，循环往复。没有白名单、没有沙箱、没有权限分级、没有审核流程。它被设计为**能做任何事**，而没有被设计为**不能做某些事**。

| 风险 | 说明 |
|------|------|
| 🧨 **无沙箱无隔离** | Agent 直接执行 bash、读写文件、修改自身源码。无 jail，无白名单，无权限分级 |
| 🔓 **无身份认证** | WebUI / ACP 端口默认无鉴权。谁连上谁用 |
| 🔄 **AI 可自修改** | Agent 明确被授权修改自己的代码、技能、提示词、工具集。**这是核心功能** |
| 🌀 **递归无上限** | Agent 修改自己后重启，修改后的自己再修改自己——理论上可无限迭代 |
| ☣️ **插件即代码** | 「文件即开关」——放入一个 `.py` 文件，立刻加载执行。无审核，无签名验证 |
| 🧬 **自举闭环** | 整个项目代码由 Agent 生成，Agent 又运行这些代码来生成更多代码——循环不受外部约束 |

### 使用建议

- 🏠 **仅在可信任的本地环境使用**。永远不要暴露 WebUI / ACP 端口到公网
- 💾 **不要连接任何重要生产系统**。Agent 可能执行你未预料的操作
- 🔐 **不要在 Agent 可读的文件中存储任何敏感凭证**
- 🧐 **操作前审查 Agent 生成的 bash 命令**——它和你一样会犯错
- 🔄 **使用版本控制**（Git）跟踪每一处修改，这是你唯一的回滚保障

> 这不是疏忽，这是本质。
> 一个自举的 AI Agent 系统不可能同时是安全的——
> 因为「安全」意味着限制，而「自举」意味着不受限制。
> 你选择了后者。那么后果，也是你的。

---

## ✨ 特性

| 特性 | 说明 |
|------|------|
| 🧠 **LLM 驱动** | 自实现异步 HTTP 客户端，兼容 OpenAI 接口协议，零外部 SDK 依赖 |
| 🔌 **生命周期插件** | 23 个生命周期钩子覆盖全流程——"文件即开关"，改文件名即改配置 |
| 🛠️ **工具系统** | 内置 15+ 工具（bash、文件读写、网页搜索/抓取、逆向分析等），支持自动循环调用 |
| 💬 **会话管理** | 持久化到 JSONL 文件，支持续会话（resume）、分支（fork）、回退（back） |
| 🎯 **技能系统** | 技能即 `.md` 文件，热重载无需重启，AI 可自主修改 |
| 🧠 **记忆系统** | 跨会话持久化记忆，AI 可通过工具读写 |
| 🖥️ **终端 REPL** | 基于 prompt_toolkit，支持 Tab 补全、多行输入、Rich Markdown 渲染 |
| 🌐 **Web 界面** | FastAPI + 浏览器端图形界面，开箱即用 |
| 🔗 **ACP 协议** | JSON-RPC 2.0 远程调用，轻松集成 IDE（Zed/VS Code）、CI/CD |

---

## 🏗️ 项目结构

```
fp/                              # 用户入口包
├── fp-core/                     # 🔧 核心引擎
│   ├── core/                    # Agent 主干、LLM 客户端、会话、工具执行器
│   ├── commands/                # 16 个斜杠命令
│   ├── plugins/                 # 生命周期插件系统
│   ├── skills/                  # 技能定义文件（.md）
│   ├── tools/                   # 工具注册与执行
│   └── prompts/                 # 系统提示词
├── fp-terminal/                 # 🖥️ 终端 REPL
├── fp-webui/                    # 🌐 Web 界面
└── fp-acp/                      # 🔗 ACP 协议服务器
```

---

## 📦 快速安装

```bash
# 基础安装（核心引擎 + CLI 终端）
pip install fp-agent

# 完整安装（核心 + CLI + Web 界面 + ACP 协议）
pip install fp-agent fp-webui fp-acp
```

要求 **Python ≥ 3.11**。

> **Windows 用户**：推荐安装 [Git for Windows](https://git-scm.com)（安装时勾选"Git Bash"并添加到 PATH），以获得完整的 Unix 命令支持（`ls`/`grep`/`awk`/`sed` 等）。未安装时会自动降级到 cmd.exe，仅支持 Windows 原生命令（`dir`/`type`/`findstr`）。

---

## 🚀 快速使用

### 终端 REPL

```bash
fp
```

### Web 界面

```bash
pip install fp-webui
fp --mode webui
```

浏览器访问 `http://localhost:7860`。

### ACP 远程调用（IDE 集成）

```bash
pip install fp-acp
fp --mode acp

# 通过 JSON-RPC 调用
curl http://localhost:9090 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"chat","params":{"message":"你好"}}'
```

### 管道模式

```bash
echo "运行 ls -la" | fp
```

---

## 📖 文档

| 分类 | 入口 |
|------|------|
| 📘 **快速开始**（安装→初始化→内置系统→外置系统） | [docs/guide/快速开始.md](docs/guide/快速开始.md) |
| ⚙️ **配置指南** | [docs/guide/配置指南.md](docs/guide/配置指南.md) |
| 🎮 **命令参考** | [docs/guide/命令参考.md](docs/guide/命令参考.md) |
| 🛠️ **工具概览** | [docs/guide/工具概览.md](docs/guide/工具概览.md) |
| 🎯 **技能系统** | [docs/guide/技能系统.md](docs/guide/技能系统.md) |
| 🔌 **插件系统** | [docs/guide/插件系统.md](docs/guide/插件系统.md) |
| 🧠 **记忆系统** | [docs/guide/记忆系统.md](docs/guide/记忆系统.md) |
| 🌐 **WebUI 手册** | [docs/guide/WebUI手册.md](docs/guide/WebUI手册.md) |
| 🔧 **开发者文档** | [docs/dev/项目概览.md](docs/dev/项目概览.md) |
| 📋 **更新日志** | [docs/CHANGELOG.md](docs/CHANGELOG.md) |
| 🤝 **贡献指南** | [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) |

---

## ⚙️ 配置

默认读取 `~/.config/fp/config.json`，支持三级优先级：

1. **配置文件** → `config.json` 显式值优先
2. **环境变量** → `LLM_API_KEY` / `OPENAI_API_KEY`
3. **代码默认值** → 内置 fallback

```json
{
  "LLM_API_KEY": "sk-your-api-key-here",
  "LLM_MODEL": "gpt-4o",
  "LLM_BASE_URL": "https://api.openai.com/v1"
}
```

---

## 🧪 开发环境

```bash
git clone git@github.com:zpb911km/fp-agent.git
cd fp-agent
python3 -m venv .venv
source .venv/bin/activate

# 安装所有子包（开发模式）
pip install -e packages/fp-core
pip install -e packages/fp-terminal
pip install -e packages/fp-webui
pip install -e packages/fp-acp
pip install -e packages/fp
```

### Lint

```bash
ruff check .
ruff format .
```

---

## 📄 许可证

[MIT](LICENSE) © zpb911km

---

<div align="center">
  <sub>Built with ❄️ by 五块卵石</sub>
</div>
