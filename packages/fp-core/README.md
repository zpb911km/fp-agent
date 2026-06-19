# fp-core — 五块卵石 Agent 引擎

[![PyPI](https://img.shields.io/pypi/v/fp-core)](https://pypi.org/project/fp-core/)
[![Python](https://img.shields.io/pypi/pyversions/fp-core)](https://pypi.org/project/fp-core/)
[![License](https://img.shields.io/pypi/l/fp-core)](LICENSE)

## 简介

**fp-core** 是五块卵石（Five Pebbles）Agent 框架的核心引擎。它提供了 Agent 生命周期管理、LLM 交互、工具执行、会话管理及插件化扩展等基础能力，是整个框架的基石。

> 如果你只是**使用** Agent，请安装 [`fp`](https://pypi.org/project/fp/) 主包；如果你要**自定义/扩展** Agent 行为，你正在正确的地方。

---

## 特性

- **生命周期钩子系统** — 在 Agent 的启动、消息处理、工具调用、响应生成等阶段注入自定义逻辑
- **插件化架构** — 通过插件动态加载工具、命令、事件监听器，无需修改核心代码
- **多 LLM 支持** — 统一的 LLM 交互接口，可接入 OpenAI、Claude、本地模型等
- **工具执行引擎** — 安全的沙箱化工具执行，支持同步/异步、超时控制、结果缓存
- **会话管理** — 持久化的会话存储与恢复，支持多会话并行
- **提示词模板** — 结构化提示词组装，支持动态注入上下文与历史

---

## 安装

```bash
pip install fp-core
```

要求 Python >= 3.11。

---

## 快速使用

### 初始化 Agent 引擎

```python
from fp_core import AgentEngine

engine = AgentEngine(config={
    "llm": {"provider": "openai", "model": "gpt-4o"},
    "session": {"storage": "file", "path": "./sessions"},
})
```

### 注册插件

```python
from fp_core.plugins import BasePlugin

class MyPlugin(BasePlugin):
    name = "my_plugin"
    
    def on_agent_start(self, ctx):
        print("Agent 启动了！")

    def on_message(self, ctx, message):
        # 在消息处理前注入逻辑
        return message

engine.plugin_manager.register(MyPlugin())
```

### 执行一次对话

```python
response = engine.chat("你好，请介绍一下你自己")
print(response)
```

---

## 架构概览

```
fp_core/
├── core/           # 核心运行时：AgentEngine, LLM 接口, 上下文管理
│   ├── agent.py        # Agent 主循环
│   ├── llm.py          # LLM 客户端抽象
│   └── context.py      # 会话上下文
├── commands/       # 内置命令（内置工具）
├── tools/          # 工具执行引擎与内置工具集
│   └── plugins/        # 工具插件加载器
├── plugins/        # 插件系统
│   └── base/           # 插件基类与钩子定义
├── prompts/        # 提示词模板管理
└── sessions/       # 会话存储与管理
```

### 生命周期钩子

插件可以通过实现以下钩子方法介入 Agent 运行流程：

| 钩子 | 触发时机 | 典型用途 |
|------|---------|---------|
| `on_agent_start` | Agent 初始化完成 | 加载自定义资源 |
| `on_agent_stop` | Agent 关闭前 | 资源清理 |
| `on_message` | 收到用户消息后 | 消息预处理/校验 |
| `before_llm_call` | 调用 LLM 前 | 注入系统提示词 |
| `after_llm_call` | 收到 LLM 响应后 | 响应后处理/过滤 |
| `before_tool_exec` | 执行工具前 | 权限检查/参数校验 |
| `after_tool_exec` | 工具执行完成后 | 结果格式化/缓存 |
| `on_response` | 生成最终响应前 | 响应润色/转译 |

---

## 依赖

| 依赖 | 版本 | 用途 |
|------|------|------|
| `httpx` | >= 0.28.0 | LLM API 异步 HTTP 请求 |
| `rich` | >= 14.0.0 | 控制台输出格式化 |
| `wcwidth` | >= 0.2.0 | 终端文本宽度计算 |
| `pyyaml` | >= 6.0 | 配置文件解析 |

---

## 扩展开发

### 编写自定义插件

```python
from fp_core.plugins import BasePlugin
from fp_core.plugins.base import hook

class LogPlugin(BasePlugin):
    name = "logger"
    
    @hook("on_message")
    def log_message(self, ctx, message):
        print(f"[LOG] 收到消息: {message[:50]}...")
        return message
    
    @hook("before_llm_call")
    def inject_system_prompt(self, ctx, messages):
        messages.insert(0, {"role": "system", "content": "请用简洁的语言回答"})
        return messages
```

### 编写自定义工具

```python
from fp_core.tools import BaseTool

class WeatherTool(BaseTool):
    name = "get_weather"
    description = "获取指定城市的天气"
    
    async def execute(self, city: str) -> dict:
        # 实现天气查询逻辑
        return {"city": city, "temperature": 22}
```

注册到 Agent：

```python
engine.tool_manager.register_tool(WeatherTool())
```

---

## 许可

MIT © zpb
