# Agent v2 - 生命周期驱动的 Agent

基于生命周期钩子的插件化 Agent 框架。

## 架构

```
agent.py (入口)
├── core/
│   ├── lifecycle.py     # 生命周期管理系统
│   └── agent.py         # Agent 主干
└── plugins/
    ├── base/
    │   └── plugin.py    # 插件基类
    ├── llm_client.py    # LLM 客户端
    ├── memory.py        # 记忆管理
    └── tool.py          # 工具执行
```

## 设计原则

1. **主干最小化**：Agent 主干只负责流程派发，不执行业务逻辑
2. **插件化**：所有功能通过生命周期钩子挂载
3. **事件驱动**：使用生命周期钩子系统实现松耦合

## 生命周期钩子

### 初始化阶段
- `ON_INIT`: Agent 初始化
- `ON_CONFIG_LOADED`: 配置加载完成

### 消息处理阶段
- `ON_MESSAGE_RECEIVED`: 收到消息
- `ON_MESSAGE_PARSE`: 消息解析
- `ON_MESSAGE_FILTER`: 消息过滤

### 执行阶段
- `ON_BEFORE_THINK`: 思考前
- `ON_THINK`: 思考中
- `ON_AFTER_THINK`: 思考后

### LLM 交互阶段
- `ON_BEFORE_LLM_CALL`: LLM 调用前
- `ON_LLM_CALL`: LLM 调用中
- `ON_AFTER_LLM_CALL`: LLM 调用后

### 响应阶段
- `ON_BEFORE_RESPONSE`: 生成响应前
- `ON_RESPONSE`: 生成响应
- `ON_AFTER_RESPONSE`: 生成响应后

### 工具执行阶段
- `ON_TOOL_SELECT`: 工具选择
- `ON_TOOL_CALL`: 工具调用
- `ON_TOOL_RESULT`: 工具结果
- `ON_TOOL_ERROR`: 工具错误

### 资源管理
- `ON_SHUTDOWN`: 关闭
- `ON_CLEANUP`: 清理资源

## 快速开始

```python
import asyncio
from agent import Agent, LLMClientPlugin, LLMConfig

async def main():
    agent = Agent(enable_log=True)
    
    # 添加 LLM 插件（mock 模式）
    llm = LLMClientPlugin(LLMConfig(provider="mock"))
    agent.add_plugin(llm)
    
    # 处理消息
    response = await agent.process("Hello!")
    print(response.content)
    
    await agent.shutdown()

asyncio.run(main())
```

## 创建自定义插件

```python
from agent import Plugin, PluginConfig, LifecycleHook, HookContext

class MyPlugin(Plugin):
    name = "my_plugin"
    
    def on_register(self, lifecycle):
        # 注册钩子
        lifecycle.register(
            LifecycleHook.ON_MESSAGE_RECEIVED,
            self.my_handler,
            priority=100,
            name="my_handler"
        )
    
    async def my_handler(self, ctx: HookContext, **kwargs) -> HookContext:
        # 处理逻辑
        print(f"Got message: {ctx.data.get('message')}")
        return ctx

# 添加到 Agent
agent.add_plugin(MyPlugin())
```

## 内置插件

- `LLMClientPlugin`: LLM 客户端，支持 OpenAI/Anthropic/Mock
- `MemoryPlugin`: 记忆管理，维护对话历史
- `ToolPlugin`: 工具执行，支持自定义工具

## 示例

```bash
# 运行示例
python examples/01_minimal.py
python examples/02_full_agent.py
python examples/03_custom_plugins.py
python examples/04_tool_example.py
```

## 运行测试

```bash
cd /media/zpb/data/codes/AI/agent_v2
python examples/01_minimal.py
```