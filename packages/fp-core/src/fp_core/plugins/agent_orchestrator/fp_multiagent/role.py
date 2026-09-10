"""AgentRole 协议 — 角色定义（duck-typed）

Agent.__init__ 用 getattr 盲取角色属性（agent.py:109 附近），因此角色可以是
任意具备下列属性的对象。本 dataclass 只是「协议」的规范载体 + JSON 编解码，
用于：父进程序列化 → 环境变量 → 子进程还原（子进程用 SimpleNamespace 即可）。

字段与 Agent 实际使用的属性一一对应：
- name          角色名（标识）
- system_prompt 系统提示词（Agent 原生支持）
- allowed_tools 工具白名单（Agent 原生过滤 get_definitions）
- llm_model     模型覆盖
- temperature   温度覆盖
- max_turns     单次会话最大轮数（保留字段，Agent 暂未消费）
- plugins       预留：worker 侧要瘦身/加载的插件集（阶段2 深化项，暂未消费）
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

# 未显式提供 system_prompt 时的兜底（避免 worker 变成「无提示词裸 agent」）
DEFAULT_WORKER_PROMPT = (
    "你是一名专注的子 agent（worker）。独立完成分配给你的子任务，"
    "直接执行、不要反问。完成后用简洁、结构化的文本汇报结论与关键依据。"
)


@dataclass
class AgentRole:
    """角色定义（协议）"""

    name: str
    system_prompt: str = ""
    allowed_tools: list[str] | None = None
    llm_model: str | None = None
    temperature: float | None = None
    max_turns: int | None = None
    plugins: list[str] | None = None

    def __post_init__(self) -> None:
        if not self.system_prompt:
            self.system_prompt = DEFAULT_WORKER_PROMPT

    # ── 编解码 ──────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentRole:
        known = {f: data[f] for f in cls.__dataclass_fields__ if f in data}
        return cls(**known)

    @classmethod
    def from_json(cls, raw: str) -> AgentRole:
        return cls.from_dict(json.loads(raw))


@dataclass
class RoleSet:
    """角色集合：支持在编排参数里用 name 引用预定义角色"""

    roles: dict[str, AgentRole] = field(default_factory=dict)

    def get(self, name: str) -> AgentRole | None:
        return self.roles.get(name)
