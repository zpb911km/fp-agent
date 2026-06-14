# Five Pebbles（五块卵石）

[![PyPI](https://img.shields.io/pypi/v/fp)](https://pypi.org/project/fp/)
[![Python](https://img.shields.io/pypi/pyversions/fp)](https://pypi.org/project/fp/)
[![License](https://img.shields.io/pypi/l/fp)](LICENSE)

## 简介

**五块卵石（Five Pebbles）** 是一个基于生命周期钩子的插件化 Agent 框架。它提供了灵活、可扩展的方式来构建和运行 AI Agent，支持终端 REPL、Web 界面和远程协议调用等多种交互方式。

> 灵感来源于《Rain World》中的迭代器「五块卵石」—— 冷静、理性、追求优雅的解决方案。

---

## 特性

- 🤖 **强大的 Agent 引擎** — 基于生命周期钩子，在 Agent 运行的各个阶段注入自定义逻辑
- 🔌 **插件化架构** — 通过插件动态扩展工具、命令和行为，无需修改核心代码
- 🖥️ **终端 REPL** — 交互式命令行界面，语法高亮、自动补全、流式输出
- 🌐 **Web 界面** — 浏览器端图形界面，开箱即用
- 🔗 **ACP 协议** — JSON-RPC 2.0 远程调用，轻松集成 IDE、CI/CD、其他 Agent

---

## 快速安装

### 基本安装（核心 + CLI）

```bash
pip install fp-agent
```

安装后即可使用终端 REPL：

```bash
fp
```

### 安装所有组件

```bash
pip install fp-agent[all]
```

### 选择性安装

```bash
# 核心 + Web 界面
pip install fp-agent[webui]

# 核心 + ACP 协议
pip install fp-agent[acp]
```

要求 Python >= 3.11。

---

## 快速使用

### 终端 REPL（默认）

```bash
fp
```

进入交互式对话：

```
>> 请用 Python 写一个斐波那契数列生成器
```

### Web 界面

```bash
pip install fp-agent[webui]
fp --webui
```

浏览器访问 `http://localhost:7860`。

### ACP 远程调用

```bash
pip install fp-agent[acp]
fp --acp
```

然后通过 JSON-RPC 调用：

```bash
curl http://localhost:9090 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"chat","params":{"message":"你好"}}'
```

---

## 配置

### 命令行选项

```bash
fp [OPTIONS]

选项：
  --config FILE        指定配置文件路径
  --webui             启动 Web 界面模式
  --acp               启动 ACP Server 模式
  --model NAME        指定 LLM 模型名称
  --host HOST         监听地址（默认 127.0.0.1）
  --port PORT         监听端口
  --debug             启用调试模式
  --quiet             静默模式
  --version           显示版本信息
```

### 配置文件

默认读取 `~/.config/fp/config.yaml`：

```yaml
llm:
  provider: openai
  model: gpt-4o
  temperature: 0.7
  api_key: ${OPENAI_API_KEY}

session:
  storage: file
  path: ~/.local/share/fp/sessions

plugins:
  - my_plugin

cli:
  theme: dark
  stream: true
```

---

## 项目结构

```
fp                          # 主包（用户入口）
├── fp-core                 # 核心引擎（Agent 逻辑、LLM 交互、插件系统）
├── fp-cli                  # 终端 REPL 界面
├── fp-webui                # Web 界面
└── fp-acp                  # ACP 通信协议（JSON-RPC 2.0）
```

---

## 文档

- [快速开始](https://github.com/zpb911km/fp-agent/blob/main/docs/guide/快速开始.md)
- [配置指南](https://github.com/zpb911km/fp-agent/blob/main/docs/guide/配置指南.md)
- [插件开发](https://github.com/zpb911km/fp-agent/blob/main/docs/guide/插件系统.md)
- [ACP 协议规范](https://github.com/zpb911km/fp-agent/blob/main/packages/fp-acp/README.md)
- [命令参考](https://github.com/zpb911km/fp-agent/blob/main/docs/guide/命令参考.md)

---

## 依赖

| 依赖 | 版本 | 说明 |
|------|------|------|
| `fp-core` | >= 0.1.0 | Agent 核心引擎（必装） |
| `fp-cli` | >= 0.1.0 | 终端 REPL（必装） |
| `fp-webui` | >= 0.1.0 | Web 界面（可选） |
| `fp-acp` | >= 0.1.0 | ACP 协议（可选） |

---

## 许可

MIT © zpb
