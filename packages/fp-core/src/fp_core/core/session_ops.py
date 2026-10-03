"""session_ops — 会话用例核心实现（api.ctl.sessions 与命令层共用）

样板归核：原「PromptBuilder 重建 + reset + load_context + replace_all」样板
在 webui(5 处)/acp(3 处) 重复手写，且各自遗漏插件注入重放；
统一收敛到本模块，重建一律经 State.rebuild_system_prompt（内置注入重放）。

SessionInfo 定义于此（core 层），由 fp_core.api.types 再导出 —— 依赖方向 api→core。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from fp_core.core.state import State

__all__ = ["SessionInfo", "SessionMeta", "clear", "fork_new", "read_messages", "switch_to"]

# 会话元数据形状与 SessionManager.list_sessions() 一致（sid → meta dict）
SessionMeta = dict[str, Any]


@dataclass(frozen=True)
class SessionInfo:
    """会话操作结果（fork_new / switch_to 返回）"""

    session_id: str
    previous_sid: str | None = None
    summary: str = ""


def _save_current(state: State) -> str:
    """落盘当前会话（save_and_summarize 统一入口；空会话回填 empty_session 摘要）"""
    old_sid = state.session_id
    summary = state.session.save_and_summarize(state.conversation.to_serializable(), old_sid)
    if not summary:
        state.session.update_meta(old_sid, summary="empty_session")
    return summary


def fork_new(state: State) -> SessionInfo:
    """新建空白会话：落盘当前 → 分配新 sid → 重建上下文（含插件注入重放）

    语义对齐原 commands/new.py，补 webui 的空会话摘要回填。
    """
    old_sid = state.session_id
    summary = _save_current(state)
    new_sid = state.session.create_session()
    state.rebuild_system_prompt()
    state.conversation.reset(state.conversation.system_prompt)
    state.session.clear_session_file()  # 惰性 sid → 写 meta-only 占位，保证 list 可见
    return SessionInfo(session_id=new_sid, previous_sid=old_sid, summary=summary)


def switch_to(state: State, sid: str) -> SessionInfo | None:
    """切换到历史会话：落盘当前 → switch → 重建并加载目标上下文

    Returns:
        SessionInfo；目标会话不存在时返回 None（由前端决定 404 或自动新建）。

    修复原分散实现的两处缺陷：
      - acp 用 replace_all(saved) 丢 system prompt（saved 不含 system 消息）
      - webui/acp 在目标为空会话时残留旧会话消息（set_messages 统一覆盖，空即清空）
    """
    old_sid = state.session_id
    summary = _save_current(state)
    if not state.session.switch_session(sid):
        return None
    state.rebuild_system_prompt()
    prompt = state.conversation.system_prompt
    saved = state.session.load_context(prompt)
    state.conversation.set_messages(prompt, saved)  # 空 saved → 仅剩 system prompt
    return SessionInfo(session_id=sid, previous_sid=old_sid, summary=summary)


def clear(state: State) -> None:
    """清空当前会话文件并重建上下文（语义对齐 commands/clear.py）"""
    state.rebuild_system_prompt()
    state.conversation.reset(state.conversation.system_prompt)
    state.session.clear_session_file()


def read_messages(sid: str) -> list[dict[str, Any]] | None:
    """只读解析会话文件为消息列表（跳过 meta 行/坏行，含 system 消息）

    Returns:
        消息列表；文件不存在返回 None。不修改实例状态。
    """
    from fp_core.core.session import session_file_path

    path = session_file_path(sid)
    if not os.path.exists(path):
        return None
    messages: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                d = cast("dict[str, Any]", parsed)
                if not d.get("__meta__"):
                    messages.append(d)
    return messages
