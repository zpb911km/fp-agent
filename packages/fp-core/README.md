<!-- ⚠️ 生成物：本文件由 scripts/check_docs_sync.py --sync 从 docs/packages/fp-core.md 生成。
     请修改主文档 docs/packages/fp-core.md，不要直接编辑本文件。 -->

# fp-core — FP 引擎内核

[![PyPI](https://img.shields.io/pypi/v/fp-core)](https://pypi.org/project/fp-core/)
[![Python](https://img.shields.io/pypi/pyversions/fp-core)](https://pypi.org/project/fp-core/)
[![License](https://img.shields.io/pypi/l/fp-core)](../../LICENSE)

> 提供 Agent 主干、生命周期钩子、LLM 通信、会话管理、工具/插件注册等基础能力；想**使用** Agent 请装 [`fp-agent`](https://pypi.org/project/fp-agent/)，要扩展行为才需要本包。

## 安装

```bash
pip install fp-core           # 仅引擎
pip install fp-agent          # 引擎 + CLI（推荐起点）
```

要求 Python ≥ 3.11。`Agent` 构造时校验 LLM 配置（`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`），缺失直接抛 `ValueError` 并指出配置文件路径；配置方式见 [配置指南](https://github.com/zpb911km/fp-agent/blob/main/docs/guide/配置指南.md)。会话关键词可选装中文分词：`pip install "fp-core[kw]"`。

## 最小示例

顶层导出即全部公共入口：`Agent`、`Message`、`Response`、`LifecycleManager`、`LifecycleHook`、`HookContext`、`Plugin`、`PluginConfig`、`PluginRegistry`。

```python
import asyncio
from fp_core import Agent
async def main() -> None:
    # 入参全部可选：enable_log / resume / session_id / io / tool_executor / prompt_builder / role / on_shutdown
    agent = Agent(enable_log=False)
    await agent.ensure_initialized()   # 触发 ON_INIT / ON_INITIALIZED，插件在此注册工具与命令
    resp = await agent.process("用一句话介绍你自己")
    print(resp.content)                # Response(content, metadata, error)
    await agent.shutdown()             # 触发 ON_SHUTDOWN / ON_CLEANUP
asyncio.run(main())
```

## 生命周期钩子

`LifecycleHook`（`fp_core/core/lifecycle.py`）共 **50 个枚举值**，与 agent loop 状态机边集一一对应。命名约定：pre 边（数据变更前）多带 `BEFORE`/`ENTER`/`EXEC`，post 边为观察点。**所有钩子都是执行钩子**——没有「通知」与「执行」之分：既能改数据，也能阻断/取消流程。

| 阶段 | 钩子 |
|------|------|
| 初始化（3） | `ON_INIT` `ON_INITIALIZED` `ON_CONFIG_LOADED` |
| 输入/命令（11） | `ON_INPUT` `ON_EMPTY` `ON_BEFORE_COMMAND` `ON_COMMAND` `ON_COMMAND_BLOCKED` `ON_FALLTHROUGH` `ON_MSG_ENTER` `ON_MESSAGE_BLOCKED` `ON_MESSAGE_FILTER` `ON_MESSAGE_RECEIVED` `ON_RESUME` |
| LLM 循环（16） | `ON_INTERRUPT` `ON_CTX_REPAIR` `ON_EARLY_EXIT` `ON_BEFORE_LLM_CALL` `ON_AFTER_LLM_CALL` `ON_CANCEL` `ON_LLM_RETRY` `ON_LLM_RETRY_OK` `ON_LLM_FATAL` `ON_STREAM_STRIP` `ON_TOOL_BLOCKED` `ON_LLM_PASS` `ON_CTX_APPEND` `ON_STREAM_INTERRUPT` `ON_TURN_END` `ON_ITERATION` |
| 工具执行（13） | `ON_TOOL_SELECT` `ON_TOOL_CANCELLED` `ON_TOOL_CALL` `ON_TOOL_REJECTED` `ON_TOOL_EXEC` `ON_TOOL_RESULT` `ON_TOOL_ERROR` `ON_TOOL_SUPPRESSED` `ON_TOOL_ERROR_PROPAGATED` `ON_RESULT_MUTATED` `ON_TOOL_RESULTS_READY` `ON_TOOL_ABORT` `ON_TOOL_PARTIAL` |
| 响应/上下文/错误/资源（7） | `ON_BEFORE_RESPONSE` `ON_CONTEXT_UPDATE` `ON_SESSION_SAVE` `ON_ERROR` `ON_CONTEXT_RESTORE` `ON_SHUTDOWN` `ON_CLEANUP` |

注册只有一条路：`lifecycle.register(hook, func, priority=100, name=None)`（`priority` 越小越先执行，`name` 是注销与诊断的键）。`emit()` 以 `func(context, **context.data)` 调用钩子，故签名写 `async def handler(ctx, **kwargs)`；就地改 `ctx.data` 或返回新 `HookContext` 都会被调用方消费。

```python
from fp_core import LifecycleHook
async def on_filter(ctx, **kwargs):          # 载荷原样展开进 kwargs，与 ctx.data 同源
    if "secret" in kwargs.get("original_content", ""):
        ctx.data["filtered_content"] = "[已过滤]"
    return ctx

agent.lifecycle.register(LifecycleHook.ON_MESSAGE_FILTER, on_filter, priority=60, name="guard_filter")
```

> 另有 `fp_core.core.lifecycle.hook` 装饰器（`@hook(LifecycleHook.ON_MESSAGE_FILTER)`）：它只写入 `_lifecycle_hook` 标记，运行时无读取方，**不自动注册**——仍须在 `on_register` 里显式 `lifecycle.register()`。

## 插件

基类在 `fp_core.plugins.base.plugin`（**`fp_core/plugins/__init__.py` 为空，`from fp_core.plugins import ...` 不存在**）。`Plugin` 是抽象基类，子类必须实现 `on_register(lifecycle)` 与 `on_unregister()`；类属性 `name`（默认 `base_plugin`）、`version` 为元信息，`PluginConfig(enabled, priority, metadata)` 提供开关与优先级。

```python
from fp_core import LifecycleHook, Plugin
class MyPlugin(Plugin):
    name = "my_plugin"
    def on_register(self, lifecycle) -> None:
        lifecycle.register(LifecycleHook.ON_INIT, self.on_init, priority=50, name="my_init")
    def on_unregister(self) -> None:
        pass
    async def on_init(self, ctx, **kwargs):
        kwargs.get("tool_registry")          # ON_INIT 由 Agent 传入本次独占的 ToolRegistry
        return ctx
```

`PluginRegistry(lifecycle, plugin_dir=...)` 扫描目录加载插件，**文件即开关**：`name.py` 加载、`name.py.disabled` 跳过、`_name.py` 视为内部模块。`unregister(name)` 连带清理该插件注册的全部钩子（`_TrackingLifecycle` 追踪）。`Agent` 实例自带 `agent.lifecycle` 与 `agent.plugins`（构造时已扫描内置 `fp_core/plugins/` 和三来源用户插件目录）；手工注册用 `PluginRegistry.register(instance)`，重名抛 `ValueError`。

## 工具注册

`fp_core.tools` 提供 `ToolRegistry`、模块级全局实例 `registry`、以及 `create_registry()`（与全局完全隔离，多 Agent 场景用它）。`register_tool` 签名固定为 `(name: str, definition: OpenAISchema, executor: ToolExecutor)`，`executor` 为 `async def (params: dict) -> str`——**工具层全异步，没有 `BaseTool` 这类基于类的工具抽象，也没有 `tool_manager`**。

```python
async def hello(params: dict) -> str:
    return f"hello, {params['who']}"
registry.register_tool(
    name="hello",
    definition={"type": "function", "function": {"name": "hello", "description": "打招呼", "parameters": {
        "type": "object", "properties": {"who": {"type": "string"}}, "required": ["who"]}}},
    executor=hello)
```

配套方法：`unregister_tool(name)`（只清动态注册条目，须与注册成对调用，避免插件禁用后残留）、`get_all_definitions()`、`await execute(tool_name, params)`。核心工具（`bash` / `read_file` / `write_file` / `edit_file`）直接绑定在 `tools/core.py`，不可插件化。

## 模块结构

```
fp_core/
├── __init__.py      # 公共导出：Agent / Message / Response / LifecycleHook / Plugin …（无 core/llm.py、core/context.py、顶层 sessions/）
├── config.py        # LLM 配置解析（LLM_API_KEY / LLM_BASE_URL / LLM_MODEL / ACTIVE_LLM）；同级 logger.py platform_utils.py
├── core/            # agent.py 编排主干｜lifecycle.py 钩子｜llm_client.py + llm_service.py 通信
│                    # conversation.py 上下文｜session*.py 会话｜tool_executor.py 执行｜handoff/jobs/state/io/prompt_builder…
├── tools/           # __init__.py（ToolRegistry）+ core.py（核心工具）+ extensions/
├── plugins/         # base/plugin.py（Plugin/PluginRegistry）+ memory/ task_system/ shortcircuit/ zeta/
├── commands/  prompts/  api/     # 斜杠命令实现、系统提示词模板、portal.py 对外门面
├── spawn.py         # spawn 契约唯一模块：FP_SPAWN_DEPTH 深度判据｜child_env 子进程环境｜declare_entry 入口声明
└── taskmap/         # 任务图模型、渲染、存取
```

## spawn 契约（`fp_core.spawn`）

核心**不含**任何派遣工具，只含「一个实例被另一个实例拉起」这件事的契约。它是纯函数/常量模块，谁都能用，核心不知道有谁在用：

| 接口 | 作用 |
|------|------|
| `declare_entry(argv)` / `entry()` | 前端声明「我被这样拉起」——替代写死 `-m fp_cli.main`，让派遣方无需知道前端是谁（依赖倒置） |
| `depth()` / `MAX_DEPTH` / `can_spawn()` | 当前实例被拉起的层级；超限拒绝再拉（防自我复刻雪崩） |
| `child_env(...)` | 构造子进程环境（不含全局 `os.environ` 污染，只带本契约字段） |
| `is_spawned()` / `source()` / `parent_sid()` / `current_sid()` | 消费方判据：我是不是被拉起的、谁拉的我、上一级会话 |
| `SILENT_ENV` / `QUIET_ENV` | 子实例静默约定：被拉起时不抢终端输出 |

前端经 `fp_core.api.spawn` 转出（协议 §5 只许导入 `fp_core.api`），核心侧与 L4 资产直接导入 `fp_core.spawn`。

## 文档

| 文档 | 内容 |
|------|------|
| [架构设计](https://github.com/zpb911km/fp-agent/blob/main/docs/dev/架构设计.md) / [引擎](https://github.com/zpb911km/fp-agent/blob/main/docs/dev/引擎.md) | 分层结构与模块职责 / 主循环与状态机 |
| [插件系统（指南）](https://github.com/zpb911km/fp-agent/blob/main/docs/guide/插件系统.md) / [开发](https://github.com/zpb911km/fp-agent/blob/main/docs/dev/插件系统.md) | 上手写插件 / §6.2 完整 50 钩子清单与 payload |
| [工具系统](https://github.com/zpb911km/fp-agent/blob/main/docs/dev/工具系统.md) / [会话模块](https://github.com/zpb911km/fp-agent/blob/main/docs/dev/会话模块.md) | 工具 schema 与执行链 / 持久化格式 |
| [LLM 通信](https://github.com/zpb911km/fp-agent/blob/main/docs/dev/LLM通信.md) / [配置系统](https://github.com/zpb911km/fp-agent/blob/main/docs/dev/配置系统.md) / [命令参考](https://github.com/zpb911km/fp-agent/blob/main/docs/guide/命令参考.md) | 请求重试与流式 / 配置位置与优先级 / 斜杠命令 |

## 依赖

| 包 | 版本 | 用途 |
|------|------|------|
| `httpx` | >=0.28.0 | 唯一网络传输层（LLM 调用、连接池管理） |
| `pyyaml` | >=6.0 | 配置文件与记忆树的 YAML 读写 |
| `jieba` | >=0.42（可选，extra `kw`） | 会话关键词中文分词；缺失自动退化为 bigram |
| `rich` / `wcwidth` | 不含 | 显示层依赖，属 `fp-terminal`；除此之外本包只用标准库 |

## License

MIT —— 见仓库根 [LICENSE](../../LICENSE)。
