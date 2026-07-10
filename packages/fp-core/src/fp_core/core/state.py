"""State — 核心状态统一访问接口

不藏不掖，命令/插件/钩子需要什么就拿什么。

设计原则：
- 不提供业务逻辑包装方法（那是调用方自己的事）
- 不限制读写能力（调用方是成年人）
- 不预测未来需要什么（接口跟随调用方生长）

历史背景：
  这个模块是从 Agent 类中剥离出来的。原本 Agent 承担了
  「主循环」+「命令代理」+「状态持有」三重职责，导致 1022 行
  的上帝类。State 承载「状态持有 + 命令访问」部分，
  Agent 回归「主循环编排」的单一职责。
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fp_core.core.conversation import ConversationState
    from fp_core.core.io import IOChannel
    from fp_core.core.lifecycle import LifecycleManager
    from fp_core.core.llm_service import LLMService
    from fp_core.core.session import SessionManager
    from fp_core.core.tool_executor import ToolExecutor
    from fp_core.plugins.base.plugin import PluginRegistry


@dataclass
class State:
    """核心状态访问接口

    命令通过此对象直接读写所有状态，无需 Agent 中转。

    当前提供：
      conversation — 消息列表（读 + 写）
      session      — 会话持久化（读取/切换/删除会话）
      llm          — LLM 调用（供 compact/shortcircuit 做摘要）
      lifecycle    — 生命周期（供插件注册钩子）
      plugins      — 插件注册表
      tool_exec    — 工具注册表（供插件注册工具）
      io           — IO 通道（供交互式命令使用）

    用法:
        # 命令内部（直接读写，不绕路）
        state.conversation.back(target_idx=3)
        state.session.save_context(state.conversation.messages)
        state.conversation.set_messages(prompt, history)
    """

    conversation: "ConversationState" = field(repr=False)
    session: "SessionManager" = field(repr=False)
    llm: "LLMService" = field(repr=False)
    lifecycle: "LifecycleManager" = field(repr=False)
    plugins: "PluginRegistry" = field(repr=False)
    tool_exec: "ToolExecutor" = field(repr=False)
    io: "IOChannel" = field(repr=False)

    # ── 便捷属性（纯委托，一行的事） ────────────────────

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def model_name(self) -> str:
        return self.llm.model

    # ── 标志位 ──────────────────────────────────────────

    # exit_bang 专用：标记核弹退出，shutdown 时删除会话
    nuclear_exit: bool = False
