<div align="center">

# FP

**一个运行时可改写自身行为的插件化 AI Agent 引擎**

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Ruff](https://img.shields.io/badge/code%20style-ruff-000000)](https://github.com/astral-sh/ruff)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](docs/CONTRIBUTING.md)

</div>

---

```text
输入 → [钩子层·挂载点] → LLM 推理 → 工具执行 → [钩子层] → 输出落盘 → 热重启
        ↑ 可改数据 · 可守卫流程 ↑              ↑  放入一个 .py 文件即生效  ↑
```

FP 不是 Chatbot 封装，而是一台**以生命周期钩子为扩展平面的 Agent 引擎**。它把"改变 Agent 行为"从改源码降维成可组合的动作：

1. **放一个文件** —— 插件挂到生命周期钩子的任意环节，注入/拦截/改写全流程
2. **改一段配置** —— 能力边界、模型路由、工具集全部外置，运行时可调
3. **让 Agent 自己做上面两件事** —— 它被明确授权改写自己的代码、提示词、工具集，并通过两段式热重启原地生效

> 命名致敬《Rain World》的迭代器「五块卵石」。本仓库的引擎、TUI、WebUI、协议层均由 FP 自己编写并持续改写——自举不是口号，是这个仓库的生成方式。

---

## 机制一览

| 机制 | 设计 |
|------|------|
| **生命周期钩子** | 挂载点覆盖 输入→推理→工具→输出→落盘→重启 全链路；执行型钩子可修改数据、可否决流程，随迭代持续扩充 |
| **自举热重启** | 两段式 `reload`：子进程隔离冒烟测试 → 动态口令门禁 → `execve` 原地替换进程，会话续接、状态不丢 |
| **工具系统** | 极小的核心工具集（bash / 读写 / 编辑）为底座，其余一切（后台任务、子 Agent、记忆、任务图、邻居协作……）都是同构插件，用户目录可继续追加 |
| **文件即行为** | 插件即 `.py` 文件，放入即被加载执行；配置天然外置，无编译、无注册仪式 |
| **LLM 层** | 零 SDK 依赖，自实现异步 HTTP 客户端，兼容 OpenAI 协议；流式 / 重试 / 超时 / think 标签提取内置 |

> 随附插件（记忆 / 任务图 / 上下文修剪 / 邻居协作等）见 [插件系统](docs/guide/插件系统.md)——它们演示了这套扩展平面的能力边界，也随时可被替换。

## 运行形态

| 形态 | 命令 | 场景 |
|------|------|------|
| 终端 REPL | `fp` | Textual TUI，全键盘操作 |
| WebUI | `fp --mode webui` | FastAPI + 浏览器界面 |
| ACP 服务 | `fp --mode acp` | JSON-RPC，IDE / CI 集成 |
| 管道 | `echo "task" \| fp` | 一行执行，脚本友好 |

---

## ⚠️ 设计取舍：默认无沙箱

FP 的能力面被刻意设计为不设限——因此**没有内置安全防护**，这是取舍而非疏漏：Agent 直接执行 bash、可修改自身源码、插件无签名验证、WebUI/ACP 端口无鉴权。仅在你的隔离环境中运行，用 Git 兜住每一次修改。若你需要的是受限沙箱 Agent，这不是你要找的项目。

---

## 📦 安装

要求 **Python ≥ 3.11**。

```bash
pip install fp-agent                 # 核心引擎 + CLI
pip install fp-agent fp-webui fp-acp # 完整形态
```

首次运行 `fp` 会进入交互式配置向导（`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`，无内置默认值）。Windows 建议安装 Git Bash 获得完整 Unix 命令支持。

## ⚙️ 配置

读取 `~/.config/fp/config.json`，优先级：配置文件 → 环境变量 → 代码默认值。

```json
{
  "LLM_API_KEY": "sk-your-api-key-here",
  "LLM_MODEL": "gpt-4o",
  "LLM_BASE_URL": "https://api.openai.com/v1"
}
```

## 🧪 开发

```bash
git clone git@github.com:zpb911km/fp-agent.git && cd fp-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -e packages/fp-core packages/fp-terminal packages/fp-webui packages/fp-acp packages/fp

ruff check . && ruff format .                  # 代码风格
python -m pytest packages/fp-core/tests -q     # 测试（conftest.py autouse fixture 隔离会话目录，不写真实数据）
```

## 🏗️ 项目结构

```
packages/
├── fp-core/        # 核心引擎：Agent 主干、LLM 客户端、会话、工具、生命周期钩子、随附插件
├── fp-terminal/    # 终端 REPL（Textual TUI + CLI）
├── fp-webui/       # Web 界面（FastAPI）
├── fp-acp/         # ACP 协议服务器（JSON-RPC）
└── fp/             # 用户入口：fp 命令、ext 资产分发、首跑向导

docs/               # guide 使用文档 · dev 开发文档 · self 自举手册
```

## 📖 文档

| 分类 | 入口 |
|------|------|
| 快速开始 / 配置 / 命令 / 工具 | [快速开始](docs/guide/快速开始.md) · [配置指南](docs/guide/配置指南.md) · [命令参考](docs/guide/命令参考.md) · [工具概览](docs/guide/工具概览.md) |
| 插件 / 记忆 / 会话 | [插件系统](docs/guide/插件系统.md) · [记忆系统](docs/guide/记忆系统.md) · [会话管理](docs/guide/会话管理.md) |
| 界面 | [WebUI 手册](docs/guide/WebUI手册.md) · [CLI 入门](docs/guide/CLI入门.md) |
| 开发 / 贡献 / 日志 | [开发者文档](docs/dev/项目概览.md) · [贡献指南](docs/CONTRIBUTING.md) · [更新日志](docs/CHANGELOG.md) |
| 总索引 | [docs/README.md](docs/README.md) |

---

## 📄 许可证

[MIT](LICENSE) © zpb911km

<div align="center">
  <sub>由 FP 自己编写 · 持续改写自身</sub>
</div>
