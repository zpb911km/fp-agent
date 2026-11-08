"""
WS 协议契约 —— 单一事实源
=====================================

**为什么存在**：事件表原先散在三处——`fp_core.core.events.EVENT_TYPES`（只覆盖 core
16 个，明确排除传输层）、`main.py` 里的直发 `send_json`、`static/app.js` 的巨型
switch（31 个 case）。三处各自演化，已经出现"后端发了前端没有 case → 静默丢弃"
的实测事故（`warning` 提示全丢）。

本模块是**整条线上的完整契约**（31 个 type，含传输层），前后端同源引用：

  后端  →  `GET /api/ui/schema` 下发本文件的 `SCHEMA`（main.py）
  前端  →  启动时拉取，据此驱动分发表；未知 type 走通用卡而非丢弃
  测试  →  `test_protocol_contract.py` 断言三方一致

**演进规则（冻结）**：新增事件只做加法——加一个 `EVENTS` 条目 + 前端一个 handler，
不允许"改了后端忘了前端"。要删事件，先把 status 标成 ``deprecated`` 过一轮，
再移除。

status 取值
  active      两侧都已接线（正常）
  unhandled   后端会发、前端暂无 case —— 事件会被忽略，属已知债务
  orphan      前端有 case、后端从无发送点 —— 历史遗留死分支
  internal    仅文档/docstring 提及，线上不出现
"""

from __future__ import annotations

from typing import Any

# ── 方向 ────────────────────────────────────────────────
S2C = "s2c"  # server → client
C2S = "c2s"  # client → server
BIDIR = "bidir"

# ── 通道（按业务归类，前端按通道决定渲染容器）────────────
CH_SESSION = "session"  # 会话 / 轮次
CH_STREAM = "stream"  # 流式卡片
CH_TOOL = "tool"  # 工具卡
CH_INTERACT = "interact"  # 交互 / 心跳
CH_LIFECYCLE = "lifecycle"  # 生命周期（重载 / 关停）

__all__ = ["SCHEMA", "EVENTS", "S2C", "C2S", "BIDIR"]


def _f(name: str, type_: str, desc: str, required: bool = True) -> dict[str, Any]:
    return {"name": name, "type": type_, "desc": desc, "required": required}


# ── 事件表 ──────────────────────────────────────────────
# 顺序即文档顺序：按通道分组。
EVENTS: list[dict[str, Any]] = [
    # ── 连接与心跳（直发，不经 EventBus）──────────────────
    {
        "type": "connected",
        "direction": S2C,
        "channel": CH_SESSION,
        "status": "active",
        "desc": "连接建立 / 重连协商结果。前端据此决定 replay 重放还是 resync 全量重拉。",
        "fields": [
            _f("sub_id", "str", "订阅标识"),
            _f("run_id", "str", "本轮运行标识，重连时用于判断是否同轮"),
            _f("seq", "int", "重连协商用的起始序号"),
            _f("session_id", "str|None", "当前会话"),
            _f("replay", "bool", "True=按 seq 从环形缓冲重放"),
            _f("resync", "bool", "True=缓冲溢出或换轮，前端须清屏并 REST 全量重拉"),
            _f("snapshot", "object", "运行时快照 {processing, pending_ask}", True),
        ],
        "emitter": "main.py:1039 (SessionRuntime.snapshot)",
    },
    {
        "type": "ping",
        "direction": BIDIR,
        "channel": CH_INTERACT,
        "status": "active",
        "desc": "心跳。后端 30s 无事件则发；前端 10s 发并据此算延迟显示。",
        "fields": [],
        "emitter": "main.py:1066 / app.js:184",
    },
    {
        "type": "pong",
        "direction": BIDIR,
        "channel": CH_INTERACT,
        "status": "active",
        "desc": "心跳应答。",
        "fields": [],
        "emitter": "main.py:1171 / app.js:287",
    },
    # ── 流式卡片 ──────────────────────────────────────────
    {
        "type": "llm_start",
        "direction": S2C,
        "channel": CH_STREAM,
        "status": "active",
        "desc": "LLM 调用开始，前端开一轮流式卡片并启动思考动画。",
        "fields": [_f("ts", "float", "时间戳")],
        "emitter": "events.py:216",
    },
    {
        "type": "llm_end",
        "direction": S2C,
        "channel": CH_STREAM,
        "status": "active",
        "desc": "LLM 调用结束。",
        "fields": [
            _f("content", "str", "本轮文本"),
            _f("has_tool_calls", "bool", "是否带工具调用"),
            _f("tool_names", "list[str]", "工具名列表（前端未读，保留）", False),
        ],
        "emitter": "events.py:218",
    },
    {
        "type": "thinking",
        "direction": S2C,
        "channel": CH_STREAM,
        "status": "unhandled",
        "desc": "思考态开关。前端实际由 llm_start 驱动动画，此事件冗余且无 case。",
        "fields": [_f("status", "str", "start | stop")],
        "emitter": "io.py:186/191",
    },
    {
        "type": "thinking_content",
        "direction": S2C,
        "channel": CH_STREAM,
        "status": "active",
        "desc": "思考 token（后端已按 THINK_FLUSH_MS 批量）。",
        "fields": [_f("token", "str", "思考文本增量")],
        "emitter": "io.py:266",
    },
    {
        "type": "chunk",
        "direction": S2C,
        "channel": CH_STREAM,
        "status": "active",
        "desc": "正文 token（后端已按 CONTENT_FLUSH_MS 批量）。",
        "fields": [_f("content", "str", "正文增量")],
        "emitter": "io.py:273",
    },
    {
        "type": "stream_end",
        "direction": S2C,
        "channel": CH_STREAM,
        "status": "active",
        "desc": "流式结束。",
        "fields": [],
        "emitter": "io.py:279",
    },
    {
        "type": "info",
        "direction": S2C,
        "channel": CH_STREAM,
        "status": "active",
        "desc": "信息提示。",
        "fields": [_f("content", "str", "提示正文")],
        "emitter": "io.py:176",
    },
    {
        "type": "warning",
        "direction": S2C,
        "channel": CH_STREAM,
        "status": "active",
        "desc": "警告提示。agent.py 有 6 处调用（插件抑制 / LLM 重试 / 工具拦截）。",
        "fields": [_f("content", "str", "警告正文")],
        "emitter": "io.py:179",
    },
    # ── 工具卡 ────────────────────────────────────────────
    {
        "type": "tool_select",
        "direction": S2C,
        "channel": CH_TOOL,
        "status": "active",
        "desc": "本轮可用工具清单。",
        "fields": [_f("tools", "list[object]", "工具描述列表")],
        "emitter": "events.py:226",
    },
    {
        "type": "tool_call",
        "direction": S2C,
        "channel": CH_TOOL,
        "status": "active",
        "desc": "工具调用开始。存在双源：EventBridge 版带 tool_call_id/args=str，"
        "WebSocketIO 直发版无 id/args=dict——前端靠缺 id 识别，属债务。",
        "fields": [
            _f("name", "str", "工具名"),
            _f("args", "str|dict", "参数（两源类型不一致，前端 typeof 兼容）"),
            _f("tool_call_id", "str|None", "调用标识；直发版缺失", False),
        ],
        "emitter": "events.py:229 / io.py:290",
    },
    {
        "type": "tool_result",
        "direction": S2C,
        "channel": CH_TOOL,
        "status": "active",
        "desc": "工具执行结果。同样双源。",
        "fields": [
            _f("name", "str", "工具名（直发版缺失）", False),
            _f("result", "str", "结果正文"),
            _f("tool_call_id", "str|None", "调用标识（直发版缺失）", False),
        ],
        "emitter": "events.py:237 / io.py:293",
    },
    # ── 交互 ──────────────────────────────────────────────
    {
        "type": "ask",
        "direction": S2C,
        "channel": CH_INTERACT,
        "status": "active",
        "desc": "向用户提问，前端渲染 ask 卡片等待回复。",
        "fields": [
            _f("ask_id", "str", "提问标识，回复时回带"),
            _f("prompt", "str", "问题正文"),
            _f("options", "list[str]", "候选选项"),
            _f("suggest", "str", "建议值"),
            _f("version", "int", "卡片版本（v2 走对象恢复）", False),
        ],
        "emitter": "io.py:323",
    },
    # ── 会话 / 轮次 ───────────────────────────────────────
    {
        "type": "queued",
        "direction": S2C,
        "channel": CH_SESSION,
        "status": "active",
        "desc": "消息已入队。",
        "fields": [_f("content", "str", "入队正文")],
        "emitter": "main.py:1109",
    },
    {
        "type": "queue_error",
        "direction": S2C,
        "channel": CH_SESSION,
        "status": "active",
        "desc": "入队失败。",
        "fields": [_f("error", "str", "错误信息")],
        "emitter": "main.py:1116",
    },
    {
        "type": "user_message",
        "direction": S2C,
        "channel": CH_SESSION,
        "status": "active",
        "desc": "用户消息回显（含跨连接恢复用的 ephemeral 标记）。",
        "fields": [
            _f("content", "str", "消息正文"),
            _f("ephemeral", "bool", "临时气泡标记", False),
            _f("ts", "float", "时间戳", False),
        ],
        "emitter": "bridge.py:143/226",
    },
    {
        "type": "history_changed",
        "direction": S2C,
        "channel": CH_SESSION,
        "status": "active",
        "desc": "历史已变更，前端标记需要刷新。",
        "fields": [_f("msg_count", "int", "消息数（前端未读）", False)],
        "emitter": "bridge.py:163",
    },
    {
        "type": "done",
        "direction": S2C,
        "channel": CH_SESSION,
        "status": "active",
        "desc": "一轮结束。异常路径额外带 error 字段。",
        "fields": [
            _f("session_id", "str", "会话标识"),
            _f("final_content", "str", "最终正文"),
            _f("non_system_count", "int", "非系统消息计数"),
            _f("from_command", "bool", "是否由斜杠命令触发", False),
            _f("error", "str|None", "异常路径错误（前端未读，已由前一条 error 事件处理）", False),
        ],
        "emitter": "main.py:287/1140/1152",
    },
    {
        "type": "cancelled",
        "direction": S2C,
        "channel": CH_SESSION,
        "status": "active",
        "desc": "本轮被取消。",
        "fields": [],
        "emitter": "main.py:295/1134/1149",
    },
    # ── 生命周期 ──────────────────────────────────────────
    {
        "type": "error",
        "direction": S2C,
        "channel": CH_LIFECYCLE,
        "status": "active",
        "desc": "错误。含 WS 认证失败（随后 close 4001）。",
        "fields": [_f("error", "str", "错误信息")],
        "emitter": "main.py:1004/1083/1095/1177, events.py:248, io.py:182",
    },
    {
        "type": "shutdown",
        "direction": S2C,
        "channel": CH_LIFECYCLE,
        "status": "active",
        "desc": "服务关停。",
        "fields": [],
        "emitter": "events.py:251",
    },
    {
        "type": "reload",
        "direction": S2C,
        "channel": CH_LIFECYCLE,
        "status": "active",
        "desc": "热重载开始。",
        "fields": [_f("message", "str", "重载说明（前端未读）", False)],
        "emitter": "main.py:644",
    },
    {
        "type": "reload_done",
        "direction": S2C,
        "channel": CH_LIFECYCLE,
        "status": "active",
        "desc": "热重载完成。",
        "fields": [
            _f("session_id", "str", "会话标识"),
            _f("model", "str", "模型名"),
        ],
        "emitter": "main.py:264/671",
    },
    # ── 死分支（前端有 case，后端从无发送点）────────────────
    {
        "type": "hint",
        "direction": S2C,
        "channel": CH_SESSION,
        "status": "orphan",
        "desc": "历史遗留，全仓无发送点。",
        "fields": [_f("content", "str", "提示正文")],
        "emitter": None,
    },
    {
        "type": "item",
        "direction": S2C,
        "channel": CH_SESSION,
        "status": "orphan",
        "desc": "历史遗留，全仓无发送点。",
        "fields": [_f("content", "str", "条目正文")],
        "emitter": None,
    },
    {
        "type": "say",
        "direction": S2C,
        "channel": CH_SESSION,
        "status": "orphan",
        "desc": "历史遗留，全仓无发送点。",
        "fields": [_f("content", "str", "正文")],
        "emitter": None,
    },
    {
        "type": "response",
        "direction": S2C,
        "channel": CH_SESSION,
        "status": "internal",
        "desc": "仅 main.py:981 docstring 提及，线上不出现。",
        "fields": [],
        "emitter": None,
    },
    # ── client → server ───────────────────────────────────
    {
        "type": "message",
        "direction": C2S,
        "channel": CH_SESSION,
        "status": "active",
        "desc": "发送用户消息。",
        "fields": [_f("content", "str", "消息正文")],
        "emitter": "app.js:602",
    },
    {
        "type": "cancel",
        "direction": C2S,
        "channel": CH_SESSION,
        "status": "active",
        "desc": "取消当前轮。",
        "fields": [],
        "emitter": "app.js:144",
    },
]


SCHEMA: dict[str, Any] = {
    "version": 1,
    "endpoint": "/ws/chat",
    "reconnect": {
        "params": ["seq", "run_id"],
        "replay_field": "replay",
        "resync_field": "resync",
        "dedupe": "前端丢弃 seq <= lastSeq；connected 豁免",
        "heartbeat": {"server_ping_s": 30, "client_ping_s": 10, "idle_close_s": 90},
    },
    "events": EVENTS,
}


def event_names() -> set[str]:
    """全部事件名（含 client→server）。"""
    return {e["type"] for e in EVENTS}


def active_names() -> set[str]:
    """两侧已接线的事件名。"""
    return {e["type"] for e in EVENTS if e["status"] == "active"}
