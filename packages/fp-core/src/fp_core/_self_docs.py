"""
内部文档 — 面向 Agent 自举的架构知识库
=======================================

此模块随 fp-core 一起发布（安装即用），专门为 Agent 在运行时了
解自身架构而设计。Agent 可以通过 import 获取结构化的架构信息，
也可以通过 self_modification 技能读取此模块的源代码。

对于更深层的细节，请查阅 GitHub 仓库中的完整文档:
  https://github.com/zpb911km/fp-agent/tree/main/docs

关键文档快捷入口:
  doc/架构设计.md      — 整体架构与设计决策
  doc/引擎.md          — Agent 引擎详解
  doc/插件系统.md      — 插件开发指南
  doc/命令系统.md      — 命令开发指南
  doc/工具系统.md      — 工具开发指南
  doc/会话模块.md      — 会话与持久化
  doc/配置系统.md      — 配置详解
  doc/自我修改.md      — 自修改机制

本模块使用 UTF-8 编码，90% 中文注释 + 10% 英文标识符。
"""

from typing import Any

# ═══════════════════════════════════════════════════════════════
# 1. 系统概览
# ═══════════════════════════════════════════════════════════════

SYSTEM_OVERVIEW = {
    "name": "五块卵石 (Five Pebbles)",
    "codename": "fp-agent",
    "description": "基于生命周期钩子的插件化 AI Agent 框架，支持终端/Web/ACP 三种交互模式",
    "design_philosophy": [
        "插件化 — 所有功能通过生命周期钩子扩展，核心不耦合具体实现",
        "可自修改 — Agent 可以在运行时修改自身源代码并重新加载",
        "会话持久化 — 每次对话自动保存，支持回退/fork/恢复",
        "跨平台 — 同时支持 Linux 和 Windows",
    ],
    "package_hierarchy": [
        {
            "package": "fp-core",
            "purpose": "引擎核心 — 生命周期、插件、命令、工具、技能、会话、LLM 通信",
            "dependents": ["fp-terminal", "fp-webui", "fp-acp", "fp-agent"],
            "install_via": "pip install fp-core",
            "source_dir": "packages/fp-core/src/fp_core/",
        },
        {
            "package": "fp-terminal",
            "purpose": "终端 REPL 界面 — prompt_toolkit 驱动的交互式命令行",
            "dependencies": ["fp-core"],
            "install_via": "pip install fp-terminal",
            "source_dir": "packages/fp-terminal/src/fp_cli/",
        },
        {
            "package": "fp-webui",
            "purpose": "Web 管理界面 — FastAPI + HTML/JS 单页",
            "dependencies": ["fp-core"],
            "install_via": "pip install fp-webui",
            "source_dir": "packages/fp-webui/src/fp_webui/",
        },
        {
            "package": "fp-acp",
            "purpose": "ACP 服务器 — JSON-RPC 2.0 协议，供 IDE 插件接入",
            "dependencies": ["fp-core"],
            "install_via": "pip install fp-acp",
            "source_dir": "packages/fp-acp/src/fp_acp/",
        },
        {
            "package": "fp-agent",
            "purpose": "主入口包 — 聚合所有子包，pip install fp-agent 一键安装",
            "dependencies": ["fp-core", "fp-terminal"],
            "optional_deps": {"webui": "fp-webui", "acp": "fp-acp"},
            "install_via": "pip install fp-agent 或 pip install fp-agent[all]",
            "source_dir": "packages/fp/src/fp/",
            "entry_point": "fp.main:main",
        },
    ],
}

# ═══════════════════════════════════════════════════════════════
# 2. 生命周期架构
# ═══════════════════════════════════════════════════════════════

LIFECYCLE = {
    "description": "事件驱动的钩子系统，是 fp-core 的扩展核心。所有插件、内置功能都通过生命周期钩子接入。",
    "hook_types": [
        {
            "type": "transform",
            "description": "可修改传入数据、守卫流程（阻止/取消）。异常会传播到调用方。",
            "hooks": ["ON_MESSAGE_FILTER", "ON_BEFORE_LLM_CALL", "ON_AFTER_LLM_CALL", "ON_BEFORE_RESPONSE"],
        },
        {
            "type": "observe",
            "description": "只通知，不改流程。异常被隔离（不会中断主流程）。",
            "hooks": [
                "ON_INIT",
                "ON_CONFIG_LOADED",
                "ON_MESSAGE_RECEIVED",
                "ON_TOOL_SELECT",
                "ON_TOOL_CALL",
                "ON_TOOL_RESULT",
                "ON_TOOL_ERROR",
                "ON_CONTEXT_UPDATE",
                "ON_ERROR",
                "ON_SHUTDOWN",
                "ON_CLEANUP",
            ],
        },
    ],
    "full_pipeline": [
        {
            "hook": "ON_INIT",
            "type": "observe",
            "when": "Agent 首次初始化时",
            "what": "资源初始化、注册其他钩子",
        },
        {
            "hook": "ON_CONFIG_LOADED",
            "type": "observe",
            "when": "配置加载完成后",
            "what": "插件可读取自定义配置项",
        },
        {
            "hook": "ON_MESSAGE_FILTER",
            "type": "transform",
            "when": "用户输入后、添加到对话前",
            "what": "修改/过滤用户输入，设置 blocked=True 可拒绝消息",
            "event_class": "MessageFilterEvent",
        },
        {
            "hook": "ON_MESSAGE_RECEIVED",
            "type": "observe",
            "when": "用户消息已加入对话",
            "what": "记录日志、统计等",
        },
        {
            "hook": "ON_BEFORE_LLM_CALL",
            "type": "transform",
            "when": "LLM 调用前",
            "what": "修改 messages/tools，设置 cancelled=True 可取消调用",
            "event_class": "BeforeLLMCallEvent",
        },
        {
            "hook": "ON_AFTER_LLM_CALL",
            "type": "transform",
            "when": "LLM 返回后",
            "what": "修改 response，设置 block_tool_execution=True 可阻止后续工具执行",
            "event_class": "AfterLLMCallEvent",
        },
        {
            "hook": "ON_TOOL_SELECT",
            "type": "observe",
            "when": "工具被选择后",
            "what": "记录被选中的工具列表",
        },
        {
            "hook": "ON_TOOL_CALL",
            "type": "observe",
            "when": "工具即将执行",
            "what": "记录工具名和参数",
        },
        {
            "hook": "ON_TOOL_RESULT",
            "type": "observe",
            "when": "工具执行完成",
            "what": "记录工具结果",
        },
        {
            "hook": "ON_TOOL_ERROR",
            "type": "observe",
            "when": "工具执行出错",
            "what": "记录错误信息",
        },
        {
            "hook": "ON_BEFORE_RESPONSE",
            "type": "transform",
            "when": "返回响应给用户前",
            "what": "修改最终回复内容",
            "event_class": "BeforeResponseEvent",
        },
        {
            "hook": "ON_CONTEXT_UPDATE",
            "type": "observe",
            "when": "对话上下文更新后",
            "what": "记录消息数等",
        },
        {
            "hook": "ON_ERROR",
            "type": "observe",
            "when": "发生错误时",
            "what": "记录/处理错误",
        },
        {
            "hook": "ON_SHUTDOWN",
            "type": "observe",
            "when": "Agent 关闭时",
            "what": "保存会话、生成摘要、显示退出面板",
        },
        {
            "hook": "ON_CLEANUP",
            "type": "observe",
            "when": "清理资源时",
            "what": "释放连接池、关闭文件等",
        },
    ],
    "module_path": "fp_core.core.lifecycle",
    "key_classes": {
        "LifecycleHook": "枚举，定义所有钩子点",
        "LifecycleManager": "管理器，注册/触发/注销钩子",
        "HookContext": "钩子执行上下文，携带 data/metadata/stop_propagation",
    },
    "typical_plugin_structure": """
# 一个典型的生命周期插件示例:
from fp_core.core.lifecycle import LifecycleHook, HookContext

class MyPlugin:
    def __init__(self, lifecycle):
        lifecycle.register(LifecycleHook.ON_MESSAGE_FILTER, self.filter_msg, priority=100)
        lifecycle.register(LifecycleHook.ON_BEFORE_LLM_CALL, self.before_llm, priority=200)
        lifecycle.register(LifecycleHook.ON_AFTER_LLM_CALL, self.after_llm, priority=100)

    async def filter_msg(self, ctx: HookContext, **kwargs) -> None:
        # kwargs 包含 content, messages
        ctx.data['filtered_content'] = kwargs['content'].strip()
        if '敏感词' in kwargs['content']:
            ctx.data['blocked'] = True
            ctx.data['block_reason'] = '您的消息包含敏感内容'

    async def before_llm(self, ctx: HookContext, **kwargs) -> None:
        # kwargs 包含 messages, tools
        ctx.data['modified_messages'] = kwargs['messages']  # 可修改
        if some_condition:
            ctx.data['cancelled'] = True
            ctx.data['cancel_reason'] = '已取消'

    async def after_llm(self, ctx: HookContext, **kwargs) -> None:
        # kwargs 包含 response, has_tool_calls, tool_names, content
        modified = dict(kwargs['response'])
        modified['content'] = kwargs['content'] + '\\n\\n[由 MyPlugin 附加]'
        ctx.data['modified_response'] = modified
""",
}


# ═══════════════════════════════════════════════════════════════
# 3. 插件系统
# ═══════════════════════════════════════════════════════════════

PLUGIN_SYSTEM = {
    "description": "插件通过生命周期钩子扩展 Agent 功能。分为内置插件和用户插件两层。",
    "architecture": {
        "base_class": "fp_core.plugins.base.plugin.PluginRegistry",
        "scan_mechanism": "自动扫描 plugin_dir 目录下所有 .py 文件，动态 import",
        "layers": [
            "内置插件: packages/fp-core/src/fp_core/plugins/ (随 fp-core 发布)",
            "用户插件: ~/.local/share/fp/plugins/ (同名覆盖内置)",
        ],
        "lifecycle_integration": "PluginRegistry 持有 LifecycleManager 引用，插件注册钩子时自动关联",
    },
    "how_to_write_a_plugin": """
# my_plugin.py — 示例插件文件
# 放在 ~/.local/share/fp/plugins/ 下

from fp_core.core.lifecycle import LifecycleHook, HookContext

def setup(registry):
    # registry 是 PluginRegistry 实例
    lifecycle = registry.lifecycle

    # 注册观察型钩子
    lifecycle.register(LifecycleHook.ON_MESSAGE_RECEIVED, on_message)

    # 注册变换型钩子
    lifecycle.register(LifecycleHook.ON_BEFORE_RESPONSE, on_before_response, priority=50)

    return "my_plugin"  # 返回插件名称

async def on_message(ctx: HookContext, **kwargs):
    content = kwargs.get('content', '')
    print(f"[MyPlugin] 用户说了: {content}")

async def on_before_response(ctx: HookContext, **kwargs):
    content = kwargs.get('content', '')
    ctx.data['modified_content'] = content + '\\n\\n💡 提示: 由 MyPlugin 添加'
""",
    "key_classes": {
        "PluginRegistry": "插件注册表，扫描目录 + 调用 setup 函数",
        "LifecycleManager": "通过 registry.lifecycle 访问",
    },
    "module_path": "fp_core.plugins.base.plugin",
}


# ═══════════════════════════════════════════════════════════════
# 4. 命令系统
# ═══════════════════════════════════════════════════════════════

COMMAND_SYSTEM = {
    "description": "斜杠命令系统（如 /help, /back, /fork），自动发现内置 + 用户命令目录",
    "architecture": {
        "discovery": "自动扫描 commands/ 目录下所有 .py 文件（排除 __init__.py）",
        "layers": [
            "内置命令: packages/fp-core/src/fp_core/commands/ (随 fp-core 发布)",
            "用户命令: ~/.local/share/fp/commands/ (同名覆盖内置)",
        ],
        "command_interface": {
            "name": "str — 命令名（如 'help'）",
            "aliases": "list[str] — 别名（可选）",
            "description": "str — 描述",
            "execute(agent, arg: str)": "async -> tuple[bool, str] — 返回 (是否已处理, 输出文本)",
        },
    },
    "all_commands": [
        {
            "name": "help",
            "aliases": ["h", "?"],
            "description": "显示帮助信息",
            "module": "fp_core.commands.help",
        },
        {
            "name": "back",
            "aliases": ["b"],
            "description": "回退到历史某条消息",
            "module": "fp_core.commands.back",
        },
        {
            "name": "clear",
            "aliases": ["c", "new"],
            "description": "清空当前会话",
            "module": "fp_core.commands.clear",
        },
        {
            "name": "compact",
            "description": "压缩对话历史（LLM 摘要）",
            "module": "fp_core.commands.compact",
        },
        {
            "name": "exit",
            "aliases": ["quit", "q"],
            "description": "正常退出",
            "module": "fp_core.commands.exit_cmd",
        },
        {
            "name": "exit!",
            "description": "核弹退出（删除当前会话不留痕迹）",
            "module": "fp_core.commands.exit_bang",
        },
        {
            "name": "fork",
            "aliases": ["f"],
            "description": "基于当前对话新建会话（fork）",
            "module": "fp_core.commands.fork",
        },
        {
            "name": "history",
            "aliases": ["hist"],
            "description": "查看对话历史",
            "module": "fp_core.commands.history",
        },
        {
            "name": "memory",
            "aliases": ["mem", "m"],
            "description": "管理长期记忆",
            "module": "fp_core.commands.memory_cmd",
        },
        {
            "name": "model",
            "description": "查看/切换 LLM 模型",
            "module": "fp_core.commands.model",
        },
        {
            "name": "resume",
            "aliases": ["r"],
            "description": "续最新会话",
            "module": "fp_core.commands.resume",
        },
        {
            "name": "session",
            "aliases": ["s"],
            "description": "会话管理（列表/切换/删除）",
            "module": "fp_core.commands.session",
        },
    ],
    "module_path": "fp_core.commands",
    "how_to_write_a_command": """
# my_cmd.py — 示例命令文件
# 放在 ~/.local/share/fp/commands/ 下

name = "greet"
aliases = ["g", "hello"]
description = "说你好"

async def execute(agent, arg: str) -> tuple[bool, str]:
    \"\"\"执行命令，返回 (是否已处理, 输出文本)\"\"\"
    if not arg:
        return (True, "你好！")
    return (True, f"你好，{arg}！")
""",
}


# ═══════════════════════════════════════════════════════════════
# 5. 工具系统
# ═══════════════════════════════════════════════════════════════

TOOL_SYSTEM = {
    "description": "LLM 可调用的工具（function calling）。分为核心工具和插件工具两层。",
    "architecture": {
        "entry_point": "fp_core.core.tool_executor.ToolExecutor",
        "registration": "ToolRegistry 维护 {tool_name: tool_def} 映射",
        "layers": [
            "核心工具: fp_core.tools.core 中的内置工具",
            "插件工具: fp_core.tools.plugins/ 目录下的文件（可热加载）",
        ],
        "tool_definition_format": {
            "type": "function",
            "function": {
                "name": "str — 工具名",
                "description": "str — 描述",
                "parameters": "{...} — JSON Schema",
            },
        },
    },
    "builtin_tools": [
        {
            "name": "bash",
            "description": "执行 shell 命令，支持管道/重定向。跨平台。超时 300 秒。",
            "module": "fp_core.tools.core",
        },
        {
            "name": "read_file",
            "description": "读取文件内容，支持指定行范围",
            "module": "fp_core.tools.core",
        },
        {
            "name": "write_file",
            "description": "创建新文件或覆盖已有文件",
            "module": "fp_core.tools.core",
        },
        {
            "name": "edit_file",
            "description": "对文件进行精确字符串替换（只替换首次出现的位置）",
            "module": "fp_core.tools.core",
        },
        {
            "name": "memory_read",
            "description": "读取跨会话长期记忆",
            "module": "fp_core.tools.plugins",
        },
        {
            "name": "memory_save",
            "description": "保存一条长期记忆",
            "module": "fp_core.tools.plugins",
        },
        {
            "name": "web_search",
            "description": "快速搜索 - 直接爬取搜索引擎返回标题+链接+摘要",
            "module": "fp_core.tools.plugins",
        },
        {
            "name": "web_fetch",
            "description": "抓取并解析网页内容，返回纯文本",
            "module": "fp_core.tools.plugins",
        },
        {
            "name": "python",
            "description": "执行 Python 代码（通过临时文件），适合复杂数据处理",
            "module": "fp_core.tools.plugins",
        },
        {
            "name": "subagent",
            "description": "派遣子 agent 执行独立任务，节省 token",
            "module": "fp_core.tools.plugins",
        },
        {
            "name": "task_create / task_update / task_list / task_clear",
            "description": "任务跟踪系统，用于多步骤工作流",
            "module": "fp_core.tools.plugins",
        },
        {
            "name": "codegraph_query",
            "description": "查询 Python 项目的代码结构、依赖关系和调用链",
            "module": "fp_core.tools.plugins",
        },
        {
            "name": "file_fingerprint",
            "description": "文件指纹识别 - 使用 file/strings 提取特征",
            "module": "fp_core.tools.plugins",
        },
        {
            "name": "smart_web_search",
            "description": "深度搜索与总结，阅读多篇文章原文后综合回答",
            "module": "fp_core.tools.plugins",
        },
        {
            "name": "vision",
            "description": "图像识别 - 发送图片到视觉模型",
            "module": "fp_core.tools.plugins",
        },
        {
            "name": "self_modification",
            "description": "修改自身源代码并测试验证",
            "module": "fp_core.tools.plugins",
        },
        {
            "name": "plugin_management",
            "description": "启用/禁用插件（文件即开关）",
            "module": "fp_core.tools.plugins",
        },
        {
            "name": "delete_empty_sessions",
            "description": "清理空会话文件",
            "module": "fp_core.tools.plugins",
        },
        {
            "name": "playwright_browser_automation",
            "description": "通过 Playwright 驱动 Chromium 实现浏览器自动化",
            "module": "fp_core.tools.plugins",
        },
    ],
    "module_path": "fp_core.tools",
    "tool_execution_flow": """
1. Agent.process() 收到用户输入
2. LLM 返回 assistant message，可能包含 tool_calls
3. ToolExecutor.execute(tc) 根据 tc.function.name 查找已注册的工具
4. 执行工具函数，返回结果字符串
5. 结果作为 tool 消息加入对话
6. LLM 再次被调用（携带工具结果），直到 LLM 不再返回 tool_calls
""",
}


# ═══════════════════════════════════════════════════════════════
# 6. 会话与持久化
# ═══════════════════════════════════════════════════════════════

SESSION_SYSTEM = {
    "description": "每次对话自动持久化到文件系统，支持回退/fork/恢复/切换",
    "architecture": {
        "store": "fp_core.core.session.SessionManager",
        "storage_format": "JSONL（每行一个 JSON 消息）",
        "storage_location": {
            "linux": "~/.local/share/fp/sessions/",
            "windows": "%LOCALAPPDATA%/fp/sessions/",
        },
        "key_operations": [
            "load_context(prompt) — 从文件恢复对话",
            "save_context(messages) — 持久化当前对话",
            "switch_session(sid) — 切换会话",
            "delete_session(sid, force) — 删除会话",
            "resume_latest() — 续最新会话",
            "list_sessions() — 列出所有会话",
            "create_session() — 创建新会话",
        ],
    },
    "session_lifecycle": """
1. Agent.__init__() 创建 SessionManager，分配新 session_id
2. 如 resume 参数，则自动恢复对应会话
3. 每次 process() 完成后，自动 save_context()
4. ON_SHUTDOWN 钩子调用 save_context() + 生成摘要
5. exit! 核弹模式跳过 save，直接 delete_session()
""",
    "conversation_state": {
        "class": "fp_core.core.conversation.ConversationState",
        "responsibilities": [
            "维护消息列表（system + user + assistant + tool 消息）",
            "add_user_message / add_assistant_message / add_tool_message",
            "back() — 回退到历史某条消息",
            "compact() — LLM 压缩摘要",
            "repair_tool_ordering() — 修复 tool call 顺序错乱",
            "replace_all() — 整体替换消息列表",
            "reset() — 清空并重新设置 system prompt",
        ],
    },
    "module_path": "fp_core.core.session",
}


# ═══════════════════════════════════════════════════════════════
# 7. 自修改机制
# ═══════════════════════════════════════════════════════════════

SELF_MODIFICATION = {
    "description": "Agent 可以在运行时修改自身源代码并验证。这是实现自我进化的关键机制。",
    "how_it_works": """
1. Agent 通过 codegraph_query 理解项目结构
2. 使用 read_file 读取目标文件
3. 使用 edit_file 或 write_file 修改代码
4. 使用 python_syntax_check (Python ast 解析) 验证语法
5. 通过 bash 运行 ruff check 和测试来验证
6. 使用 git diff 确认修改内容
7. 使用 git commit 提交（如需要）
""",
    "entry_points": [
        "fp-core: packages/fp-core/src/fp_core/ — 核心引擎（生命周期/插件/命令/工具/技能）",
        "fp-terminal: packages/fp-terminal/src/fp_cli/ — 终端界面",
        "fp-webui: packages/fp-webui/src/fp_webui/ — Web 界面",
        "fp-acp: packages/fp-acp/src/fp_acp/ — ACP 服务器",
        "fp-agent: packages/fp/src/fp/ — 主入口",
    ],
    "validation_commands": [
        "ruff check . — lint 检查",
        "ruff format --check . — 格式化检查",
        "python -c 'import ast; ast.parse(open(f).read())' — 语法检查",
        "git diff — 查看修改",
    ],
    "module_path": "fp_core.tools.plugins.self_modification_plugin",
    "example_patch_workflow": """
# 典型的自修改流程:
1. codegraph_query → 理解要修改的模块结构
2. read_file → 读取源码
3. edit_file → 精确修改
4. bash 'ruff check . && ruff format --check .' → 验证
5. bash 'python -m build packages/fp-core' → 构建验证
6. git diff → 最终确认
""",
}


# ═══════════════════════════════════════════════════════════════
# 8. 配置系统
# ═══════════════════════════════════════════════════════════════

CONFIG_SYSTEM = {
    "description": "通过环境变量配置，支持 .env 文件",
    "config_items": {
        "LLM_API_KEY": {"required": True, "description": "LLM API 密钥"},
        "LLM_API_BASE_URL": {"required": True, "default": "https://api.openai.com/v1", "description": "LLM API 地址"},
        "LLM_MODEL": {"required": False, "default": "gpt-4o", "description": "模型名"},
        "LLM_MAX_TOKENS": {"required": False, "default": "4096", "description": "最大 token 数"},
        "LLM_TEMPERATURE": {"required": False, "default": "0.7", "description": "温度参数"},
        "SESSIONS_DIR": {"required": False, "description": "会话存储目录（跨平台默认）"},
        "MEMORY_DIR": {"required": False, "description": "长期记忆存储目录（跨平台默认）"},
    },
    "platform_directories": {
        "linux": {
            "config": "~/.config/fp/",
            "data": "~/.local/share/fp/",
            "sessions": "~/.local/share/fp/sessions/",
            "memories": "~/.local/share/fp/memories/",
            "plugins": "~/.local/share/fp/plugins/",
            "commands": "~/.local/share/fp/commands/",
        },
        "windows": {
            "config": "%APPDATA%/fp/",
            "data": "%LOCALAPPDATA%/fp/",
            "sessions": "%LOCALAPPDATA%/fp/sessions/",
            "memories": "%LOCALAPPDATA%/fp/memories/",
            "plugins": "%LOCALAPPDATA%/fp/plugins/",
            "commands": "%LOCALAPPDATA%/fp/commands/",
        },
    },
    "module_path": "fp_core.config",
}


# ═══════════════════════════════════════════════════════════════
# 9. LLM 通信抽象层
# ═══════════════════════════════════════════════════════════════

LLM_ABSTRACTION = {
    "description": "两层抽象：低层 Client（httpx）+ 高层 LLMService（重试/降级/logprobs 支持）",
    "layers": [
        {
            "name": "Client",
            "class": "fp_core.core.llm_client.Client",
            "role": "裸 HTTP 通信，处理 API Key、Base URL、流式/非流式请求",
            "transport": "httpx.AsyncClient（连接池复用）",
        },
        {
            "name": "LLMService",
            "class": "fp_core.core.llm_service.LLMService",
            "role": "业务逻辑层：重试/降级/logprobs/tool_choice 管理",
            "features": [
                "自动重试（指数退避）",
                "模型降级（主模型失败 → 备用模型）",
                "logprobs 提取",
                "tool_choice 管理",
            ],
        },
    ],
    "communication_flow": """
1. Agent._invoke_llm() 构造 messages + tools
2. LLMService.chat() 处理重试/降级逻辑
3. Client.chat() 发送 HTTP 请求到 API
4. 返回 assistant message（含 content + tool_calls）
5. Agent 提取 tool_calls 交给 ToolExecutor 执行
6. 循环直到 LLM 不再返回 tool_calls
""",
    "module_paths": {
        "client": "fp_core.core.llm_client",
        "service": "fp_core.core.llm_service",
    },
}


# ═══════════════════════════════════════════════════════════════
# 10. Agent 主循环（完整流程图）
# ═══════════════════════════════════════════════════════════════

AGENT_MAIN_LOOP = """
用户输入
   │
   ├─ 是斜杠命令? ───→ execute_command() ───→ 回复
   │
   ▼
ON_MESSAGE_FILTER (transform — 可过滤/修改/阻止)
   │
   ▼
添加用户消息到对话
   │
   ▼
ON_MESSAGE_RECEIVED (observe — 仅通知)
   │
   ▼
┌─────────────────────────────────────┐
│             主循环开始              │
│                                     │
│  ON_BEFORE_LLM_CALL (transform)     │
│       │                             │
│       ▼                             │
│  LLMService.chat() ←→ Client        │
│       │                             │
│       ▼                             │
│  ON_AFTER_LLM_CALL (transform)      │
│       │                             │
│       ▼                             │
│  有 tool_calls? ────否──→ 跳出循环  │
│       │                             │
│       是                            │
│       ▼                             │
│  ON_TOOL_SELECT → ON_TOOL_CALL      │
│       │                             │
│       ▼                             │
│  ToolExecutor.execute()             │
│       │                             │
│       ▼                             │
│  ON_TOOL_RESULT / ON_TOOL_ERROR     │
│       │                             │
│       添加 tool 消息到对话           │
│       │                             │
│       └──→ 继续循环                  │
└─────────────────────────────────────┘
   │
   ▼
ON_CONTEXT_UPDATE (observe)
   │
   ▼
保存会话到文件
   │
   ▼
ON_BEFORE_RESPONSE (transform — 可修改回复)
   │
   ▼
返回 Response
"""


# ═══════════════════════════════════════════════════════════════
# 11. GitHub 资源指引
# ═══════════════════════════════════════════════════════════════

GITHUB_RESOURCES = {
    "repository": "https://github.com/zpb911km/fp-agent",
    "documentation_root": "https://github.com/zpb911km/fp-agent/tree/main/docs",
    "key_documents": {
        "架构设计": "docs/dev/架构设计.md",
        "引擎": "docs/dev/引擎.md",
        "插件系统": "docs/dev/插件系统.md",
        "命令系统": "docs/dev/命令系统.md",
        "工具系统": "docs/dev/工具系统.md",
        "会话模块": "docs/dev/会话模块.md",
        "配置系统": "docs/dev/配置系统.md",
        "数据持久化": "docs/dev/数据持久化.md",
        "中断机制": "docs/dev/中断机制.md",
        "自我修改": "docs/dev/自我修改.md",
        "显示层": "docs/dev/显示层.md",
        "文件命名约定": "docs/dev/文件命名约定.md",
        "项目概览": "docs/dev/项目概览.md",
    },
    "guides": {
        "快速开始": "docs/guide/快速开始.md",
        "CLI入门": "docs/guide/CLI入门.md",
        "命令参考": "docs/guide/命令参考.md",
        "记忆系统": "docs/guide/记忆系统.md",
        "会话管理": "docs/guide/会话管理.md",
        "配置指南": "docs/guide/配置指南.md",
        "插件系统": "docs/guide/插件系统.md",
        "工具概览": "docs/guide/工具概览.md",
        "WebUI手册": "docs/guide/WebUI手册.md",
        "FAQ": "docs/guide/FAQ.md",
    },
    "acp_docs": {
        "README": "docs/acp/README.md",
    },
    "other": {
        "CHANGELOG": "docs/CHANGELOG.md",
        "CONTRIBUTING": "docs/CONTRIBUTING.md",
    },
    "issues": "https://github.com/zpb911km/fp-agent/issues",
    "discussions": "https://github.com/zpb911km/fp-agent/discussions",
}


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════


def get_summary() -> str:
    """获取一段简短的架构说明（适合注入 system prompt）"""
    return (
        "我是五块卵石 (Five Pebbles)，一个基于生命周期钩子的插件化 AI Agent 框架。\n"
        "我的运行时文档存储在 fp_core._self_docs 模块中，\n"
        "可以通过 `from fp_core._self_docs import *` 或 `import fp_core._self_docs as docs` 导入。\n"
        f"完整文档: {GITHUB_RESOURCES['documentation_root']}"
    )


def get_all_sections() -> dict[str, Any]:
    """返回所有章节的汇总字典"""
    return {
        "system_overview": SYSTEM_OVERVIEW,
        "lifecycle": LIFECYCLE,
        "plugin_system": PLUGIN_SYSTEM,
        "command_system": COMMAND_SYSTEM,
        "tool_system": TOOL_SYSTEM,
        "session_system": SESSION_SYSTEM,
        "self_modification": SELF_MODIFICATION,
        "config_system": CONFIG_SYSTEM,
        "llm_abstraction": LLM_ABSTRACTION,
        "github_resources": GITHUB_RESOURCES,
    }


def find_tool(name: str) -> dict | None:
    """按名称查找工具信息"""
    for t in TOOL_SYSTEM["builtin_tools"]:
        if t["name"] == name or name in t["name"]:
            return t
    return None


def find_command(name: str) -> dict | None:
    """按名称查找命令信息"""
    for c in COMMAND_SYSTEM["all_commands"]:
        if c["name"] == name or name in c.get("aliases", []):
            return c
    return None


def find_hook(name: str) -> dict | None:
    """按钩子名查找生命周期信息"""
    for h in LIFECYCLE["full_pipeline"]:
        if h["hook"] == name:
            return h
    return None


def get_github_url(doc_path: str) -> str:
    """根据文档相对路径生成完整 GitHub URL"""
    return f"{GITHUB_RESOURCES['repository']}/tree/main/{doc_path}"
