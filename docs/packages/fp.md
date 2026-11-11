# fp-agent — FP 的安装入口与命令行

[![PyPI](https://img.shields.io/pypi/v/fp-agent)](https://pypi.org/project/fp-agent/)
[![Python](https://img.shields.io/pypi/pyversions/fp-agent)](https://pypi.org/project/fp-agent/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/zpb911km/fp-agent/blob/main/LICENSE)

> FP 是一个基于生命周期钩子的插件化 AI Agent 框架 —— 灵感来自《Rain World》的迭代器「五块卵石」。

`fp-agent` 是 FP 的**唯一命令入口**：本身不含引擎，只解析顶层参数、做首跑配置门禁，再把控制权交给对应前端（`fp-terminal` / `fp-webui` / `fp-acp`）。

## 安装

要求 **Python ≥ 3.11**。

```bash
pip install fp-agent            # 基础：自动拉取 fp-core（引擎）+ fp-terminal（终端 REPL）
pip install "fp-agent[webui]"   # + Web 界面（fp-webui）
pip install "fp-agent[acp]"     # + ACP 协议（fp-acp）
pip install "fp-agent[all]"     # 全部
```

Windows 上必须安装 **Git for Windows**（安装时勾选 Git Bash）。FP 强制使用 Git Bash：找不到时**不会降级到 cmd.exe**，而是报错并给出安装指引；也可用配置项 `BASH_PATH` 显式指定 `bash.exe` 路径。

## 快速开始

```bash
fp               # 交互式终端 REPL（默认 cli 模式）
fp -m "你好"     # 单次消息，答完即退
fp --mode webui  # 浏览器界面，默认 http://127.0.0.1:8765
fp --mode acp    # ACP 服务，走 stdio（无端口）
```

**首跑配置门禁**（先于任何前端启动）：

- 交互终端缺配置 → 进入向导，依次询问 **API 基础地址 → 模型名称 → API 密钥**（三项均必填，向导不给任何示例或默认值）；写盘后**本次启动立即生效**，无需重启。
- 非交互场景（管道、`--headless`、`-m`、无 tty 的 `webui`、`acp`、subagent 子进程）→ 不阻塞，打印含配置文件绝对路径的指引并以**退出码 1** 退出。
- `acp` **一律不进向导**（其 stdin 是 IDE 的 JSON-RPC 协议流）；`--version` / `--update` 跳过门禁。

## 命令行

顶层参数（由 `fp/main.py` 注册，其余参数原样透传给前端）：

| 参数 | 说明 |
|------|------|
| `--mode`, `-m` | 启动模式，取值 `cli`（默认）/ `webui` / `acp` |
| `--model` | 覆盖本次激活模型：`provider/model`，或 `LLM_PROVIDERS` 表内裸模型名；未命中则回退配置激活项并告警 |
| `--session SID` | 恢复指定会话（cli 等价 `-r SID`；webui/acp 经 `FP_RELOAD_SID` 同样生效） |
| `--docs` | 查看离线文档（等价 `fp docs`） |
| `--version` | 打印版本号（`fp <fp-core 版本>`）并退出 |
| `--update`, `-u` | 检查并升级本地已安装的 fp 组件（缺失的包不强装，单包失败不阻塞其余） |

子命令（在顶层解析前路由，参数互不干扰）：

- `fp docs [路径 | --list | --path | --open]` —— 离线文档（见下节）
- `fp ext <子命令>` —— 扩展资产分发：工具 / 命令 / 插件（详见 `fp ext -h`）

透传给前端的参数：

| 模式 | 参数 |
|------|------|
| `cli` | `-m/--message MSG` 单次消息；`-r/--resume [SID]` 恢复会话；`--headless` 无头驻留 |
| `webui` | `--host`（默认 `127.0.0.1`）、`--port`（默认 `8765`）、`--expose`（监听 `0.0.0.0`）、`--reload`（热重载） |
| `acp` | 无参数；JSON-RPC 2.0 走 **stdio**，不监听端口 |

> `-m` 是双义短选项：取值命中 `cli|webui|acp` 时视为 `--mode`，否则视为 `--message`。

## 配置

配置文件 `~/.config/fp/config.json`（Windows：`%APPDATA%\fp\config.json`）；运行期数据（会话 / 记忆 / 日志）在 `~/.local/share/fp/`。

```json
{
  "LLM_API_KEY": "sk-...",
  "LLM_API_BASE_URL": "https://api.deepseek.com/v1",
  "LLM_MODEL": "deepseek-chat"
}
```

上述三键均**必填**——FP 不含任何内置连接默认值。可选键：`TEMPERATURE`、`MAX_TOKENS`、`TIMEOUT`、`RETRY_COUNT`、`MAX_ITERATIONS`、`MAX_CONTEXT_TOKENS`、`ACTIVE_LLM`、`LLM_PROVIDERS`（供应商 / 模型两级表）、`BASH_PATH`。

**优先级：配置文件显式值 > 环境变量 > 内置默认**（环境变量与键同名）。完整字段与校验规则见 [配置指南](../guide/配置指南.md)。

## 随包离线文档

发布时仓库 `docs/` 会被注入 `site-packages/fp/docs/`，随 wheel 分发，装完即可离线查阅：

```bash
fp docs                     # 用法与文档目录（等价 fp --docs）
fp docs --list              # 文档目录树
fp docs --path              # 仅打印文档目录绝对路径
fp docs self/README.md      # 直接打印某篇（纯文本走 stdout）
fp docs --open              # 用系统默认程序打开目录（人类交互用）
```

默认**绝不自动调用 GUI**：输出纯文本、不阻塞，供 agent 自举时读取。

## 文档

| 文档 | 内容 |
|------|------|
| [快速开始](../guide/快速开始.md) | 安装、首配、内置系统全貌 |
| [CLI 入门](../guide/CLI入门.md) | 终端 REPL 操作与按键 |
| [命令参考](../guide/命令参考.md) | 16 个斜杠命令速查 |
| [WebUI 手册](../guide/WebUI手册.md) | 浏览器界面使用 |
| [配置指南](../guide/配置指南.md) | config.json 全字段与优先级 |
| [插件系统](../guide/插件系统.md) | 生命周期钩子与插件开发 |
| [工具概览](../guide/工具概览.md) | 内置工具一览 |
| [引擎](../dev/引擎.md) | Agent 主循环与钩子挂载点 |
| [架构设计](../dev/架构设计.md) | 分层与数据流 |
| [自举索引](../self/README.md) | 能力边界 + 查找地图 |

> 上表为仓库内相对路径；PyPI 不解析相对链接，请对照 [GitHub 文档目录](https://github.com/zpb911km/fp-agent/tree/main/docs) 阅读。

## 依赖

| 包 | 版本 | 角色 |
|----|------|------|
| `fp-core` | `>= 0.2.0` | Agent 核心引擎（必装） |
| `fp-terminal` | `>= 0.2.0` | 终端 REPL（必装） |
| `fp-webui` | `>= 0.2.0` | Web 界面（extra `webui`） |
| `fp-acp` | `>= 0.2.0` | ACP 协议（extra `acp`） |
| Python | `>= 3.11` | 运行时 |

## License

MIT © zpb —— 见 [LICENSE](https://github.com/zpb911km/fp-agent/blob/main/LICENSE)。
