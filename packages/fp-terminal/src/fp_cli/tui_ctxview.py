"""上下文视图投影 — ``transcript`` → 语义单元（纯函数，无 Textual 依赖）

## 契约

消息区恒等于 ``portal.run.transcript`` 的**非首条 system** 投影。首条 system
是系统提示词（可能上万 token，从不进视图），这是「看见 == 实际」的既定边界；
其余消息——包括工具调用/返回、后续 system（``⁂[kind]`` 注入标记）——都必须在
视图里有对应物。

## 为什么在这里做投影而不是在渲染层

对账判据要看「**当前投影**是否仍是上次确认投影的**尾部追加**」（见
``FPApp._sync_view``）。这个判据只依赖消息内容，与事件流无关，因此可以放在
纯函数层：给定任意 ``messages`` 都能算出同一指纹序列，便于单测且不引 Textual。

关键设计：**不认消息来源**。命令（``/sc`` ``/back`` ``/resume``…）、工具
（``shortcircuit`` 退化/合并）、插件、reload 续接、外部客户端改写，只要落在
``conversation.messages`` 上，投影就变，对账就成立——机制里没有任何命令名、
工具名或 ``op`` 语义，新增改写路径自动覆盖。

## 指纹稳定性

``key`` 只由**内容**算出（角色 + 正文摘要 / 工具名 + 参数摘要 / tool_call
id），不含消息下标。于是「中段删除若干消息」后剩余单元的 key 不变，尾部单元
仍能与之对齐——前缀判断因此对「任意位置删块」都成立。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, cast

#: 单元种类——渲染层按种类分派（见 ``FPApp._unit_widgets``）
KIND_USER = "user"
KIND_ASSISTANT = "assistant"
KIND_THINK = "think"
KIND_TOOL_CALL = "tool_call"
KIND_TOOL_RESULT = "tool_result"
#: 兜底种类：未知 role / 未知结构。**必须渲染**，否则视图 ≠ 上下文。
KIND_OTHER = "other"


@dataclass(frozen=True, slots=True)
class Unit:
    """投影出的单个可视单元（≈ 视图里的一块）。

    Attributes:
        kind: 单元种类（``KIND_*``）
        key: 内容指纹，用于前缀对齐与「视图是否已同步」判定
        text: 正文 / 工具返回 / 未知角色的内容
        name: 工具名 / 未知角色名
        args: 工具调用参数（已解析的 dict；解析失败则为原文）
        chars: 思考字数（渲染折叠态摘要用）
    """

    kind: str
    key: str
    text: str = ""
    name: str = ""
    args: Any = None
    chars: int = 0


def digest(text: str) -> str:
    """稳定内容摘要（16 位十六进制）——同内容必同 key，跨进程/跨重启一致。

    不用内置 ``hash()``：它受 PYTHONHASHSEED 影响，进程间不稳定。
    """
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def _as_dict(raw: object) -> dict[str, Any] | None:
    """``object`` → ``dict[str, Any]``（非 dict 返回 None）

    这是本模块唯一的类型收敛口：所有消息/工具载荷都从这里窄化，避免
    ``isinstance(x, dict)`` 直接作用在 ``Any`` 上产生 ``dict[Unknown, Unknown]``。
    """
    if not isinstance(raw, dict):
        return None
    return cast("dict[str, Any]", raw)


def _as_list(raw: object) -> list[object] | None:
    """``object`` → ``list[object]``（非 list 返回 None）"""
    if not isinstance(raw, list):
        return None
    return cast("list[object]", raw)


def _text_of(message: dict[str, Any]) -> str:
    """消息正文 → str（content 可能是 str / None / 结构化 parts 列表）"""
    raw = message.get("content")
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(raw)


def _parse_args(raw: object) -> Any:
    """tool_call 参数 → dict；已是 dict 直接用，JSON 字符串则解析，失败保留原文"""
    data = _as_dict(raw)
    if data is not None:
        return data
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw
    return raw


def _args_repr(args: Any) -> str:
    """参数 → 稳定字符串（排序键，跨调用可比）"""
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(args)


def _tool_calls(message: dict[str, Any]) -> list[Unit]:
    """assistant 消息里的 ``tool_calls`` → 单元列表（兼容嵌套与扁平两种形状）"""
    calls = _as_list(message.get("tool_calls"))
    if calls is None:
        return []
    units: list[Unit] = []
    for raw_item in calls:
        item = _as_dict(raw_item)
        if item is None:
            # 形状不可辨：不猜、不丢——按兜底单元渲染原文
            units.append(
                Unit(KIND_OTHER, key=f"other:tool_call:{digest(str(raw_item))}", name="tool_call", text=str(raw_item))
            )
            continue
        call_id = str(item.get("id") or "")
        func = _as_dict(item.get("function")) or item
        name = str(func.get("name") or "")
        args = _parse_args(func.get("arguments", func.get("args")))
        # id 优先（同一工具被调用多次时参数可能完全相同，只有 id 能区分）
        ident = call_id or f"{name}:{digest(_args_repr(args))}"
        units.append(Unit(KIND_TOOL_CALL, key=f"tool_call:{ident}", name=name, args=args))
    return units


def project(messages: object) -> list[Unit]:
    """transcript → 单元序列（视图的唯一渲染依据）。

    首条 system 视为系统提示词跳过；其余消息一律产出单元，未知角色走
    ``KIND_OTHER`` 兜底——**宁可画得朴素，也不静默丢弃**：漏一个单元，
    视图就不再等于上下文。
    """
    items = _as_list(messages)
    if items is None:
        return []
    units: list[Unit] = []
    for index, raw_item in enumerate(items):
        message = _as_dict(raw_item)
        if message is None:
            units.append(Unit(KIND_OTHER, key=f"other:raw:{digest(str(raw_item))}", text=str(raw_item)))
            continue
        role = str(message.get("role") or "")
        text = _text_of(message)

        if role == "system" and index == 0:
            continue  # 系统提示词：视图的既定边界（从不显示）

        if role == "user":
            units.append(Unit(KIND_USER, key=f"user:{digest(text)}", text=text))
        elif role == "assistant":
            reasoning = message.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                units.append(Unit(KIND_THINK, key=f"think:{digest(reasoning)}", text=reasoning, chars=len(reasoning)))
            if text:
                units.append(Unit(KIND_ASSISTANT, key=f"assistant:{digest(text)}", text=text))
            units.extend(_tool_calls(message))
            if not reasoning and not text and not message.get("tool_calls"):
                # 空 assistant（异常/中断残留）：仍占位，保住条数对齐
                units.append(Unit(KIND_ASSISTANT, key="assistant:empty", text=""))
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            ident = call_id or digest(text)
            units.append(Unit(KIND_TOOL_RESULT, key=f"tool_result:{ident}", text=text))
        else:
            label = role or "unknown"
            units.append(Unit(KIND_OTHER, key=f"other:{label}:{digest(text)}", name=label, text=text))
    return units


def fingerprint(units: list[Unit]) -> tuple[str, ...]:
    """单元序列 → 指纹序列（对账的比较对象）"""
    return tuple(unit.key for unit in units)


def is_append_only(settled: tuple[str, ...], current: tuple[str, ...]) -> bool:
    """``current`` 是否只是 ``settled`` 的**尾部追加**。

    True = 视图可继续由事件流自行渲染（无需干预）；
    False = 中段被删改 / 内容被替换 / 换源（``/resume``）→ 必须重建视图。

    只看前缀，不做 LCS：改写只会「删块 / 插块 / 换内容」，不会重排；
    任一单元内容变化即 key 不同，前缀立刻断裂。

    **空基线必须单独判**：``settled == ()`` 的含义是「视图里没有任何上下文单元」
    （新会话启动，或只存了 system prompt），它不构成「已呈现的内容」。若对空
    基线套前缀判据，``current[:0] == ()`` 恒成立 → 任何换源都被误判为「纯尾部
    追加」，基线被静默推进而视图永不重建（现象：新会话里 ``/resume`` 之后
    消息区仍是欢迎屏）。故空基线只在**投影也为空**时才算一致。
    """
    if not settled:
        return not current
    return len(current) >= len(settled) and current[: len(settled)] == settled


def message_count(messages: object) -> int:
    """非首条 system 的消息条数（= 视图应呈现的消息数，给提示行用）"""
    items = _as_list(messages)
    if items is None:
        return 0
    total = 0
    for index, raw_item in enumerate(items):
        message = _as_dict(raw_item)
        if message is not None and message.get("role") == "system" and index == 0:
            continue
        total += 1
    return total
