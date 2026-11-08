"""
协议契约三方一致性
=====================================

盯住三个会各自演化的事实源，任何一方漂移即失败：

  A. `fp_core.core.events.EVENT_TYPES`  —— core 发布的事件（只读引用，webui 不改 core）
  B. `fp_webui.protocol.EVENTS`         —— 本模块的完整契约（单一事实源）
  C. `static/app.js` 的 `handleEvent`   —— 前端实际分发的 case

对应关系（详见 protocol.py docstring）：
  A ⊆ B              core 事件必须登记进契约
  active 的 s2c ⊆ C   前端必须能接住所有该发的事件
  C ⊆ B              前端不许处理契约外的事件（防止野事件）
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fp_webui.protocol import BIDIR, C2S, EVENTS, S2C, SCHEMA

_STATIC = Path(__file__).resolve().parents[1] / "src" / "fp_webui" / "static"
_APP_JS = _STATIC / "app.js"


def _frontend_cases() -> set[str]:
    """抽出 `handleEvent` 函数体内的所有 case 标签。

    按花括号配对截取函数体，避免误吞 app.js 里其他 switch。
    """
    src = _APP_JS.read_text(encoding="utf-8")
    start = src.index("function handleEvent")
    open_brace = src.index("{", start)

    depth = 0
    end = open_brace
    for i in range(open_brace, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break

    body = src[start:end]
    return set(re.findall(r"case\s+'([A-Za-z0-9_]+)'", body))


def _core_event_types() -> set[str]:
    from fp_core.core.events import EVENT_TYPES

    return set(EVENT_TYPES)


def _by_status(status: str) -> set[str]:
    return {e["type"] for e in EVENTS if e["status"] == status}


# ── B 本身：结构自洽 ─────────────────────────────────────


def test_no_duplicate_event_names():
    names = [e["type"] for e in EVENTS]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"事件名重复: {dupes}"


def test_event_shape():
    for e in EVENTS:
        assert e["type"], "事件缺 type"
        assert e["direction"] in (S2C, C2S, BIDIR), f"{e['type']} 方向非法: {e['direction']}"
        assert e["channel"], f"{e['type']} 缺 channel"
        assert e["status"] in ("active", "unhandled", "orphan", "internal"), f"{e['type']} status 非法: {e['status']}"
        assert isinstance(e["fields"], list), f"{e['type']} fields 应为 list"


def test_schema_is_json_serializable():
    """前端要靠 /api/ui/schema 拉它，必须能直接 json.dumps。"""
    text = json.dumps(SCHEMA, ensure_ascii=False)
    assert "events" in text
    assert json.loads(text)["version"] == SCHEMA["version"]


def test_c2s_events_are_not_expected_as_cases():
    """client→server 的事件由前端**发送**，不该出现在分发表里。"""
    c2s = {e["type"] for e in EVENTS if e["direction"] == C2S}
    assert c2s, "契约里没有 c2s 事件，测试前提失效"
    assert c2s == {"message", "cancel"}


# ── A ⊆ B：core 事件必须登记 ─────────────────────────────


def test_core_events_are_registered():
    core = _core_event_types()
    contract = {e["type"] for e in EVENTS}
    missing = core - contract
    assert not missing, (
        f"core 发布了但契约未登记的事件: {sorted(missing)} → 请在 fp_webui/protocol.py 的 EVENTS 里补条目（只做加法）"
    )


# ── active s2c ⊆ C：前端必须接得住 ───────────────────────


def test_active_s2c_events_have_frontend_case():
    cases = _frontend_cases()
    active_s2c = {e["type"] for e in EVENTS if e["direction"] in (S2C, BIDIR) and e["status"] == "active"}
    missing = active_s2c - cases
    assert not missing, (
        f"后端会发、前端无 case（会被静默丢弃）: {sorted(missing)} "
        f"→ 在 app.js handleEvent 补 case，或把 protocol.py 的 status 改成 unhandled"
    )


def test_unhandled_and_orphan_are_documented():
    """标 unhandled/orphan 的事件必须写清理由，防止悄悄变成黑洞。"""
    for e in EVENTS:
        if e["status"] in ("unhandled", "orphan", "internal"):
            assert e["desc"], f"{e['type']} 标了 {e['status']} 但 desc 为空"
            assert len(e["desc"]) > 8, f"{e['type']} 的 desc 过短，未说明去向"


# ── C ⊆ B：不许处理契约外的事件 ──────────────────────────


def test_no_undocumented_frontend_cases():
    cases = _frontend_cases()
    contract = {e["type"] for e in EVENTS}
    stray = cases - contract
    assert not stray, f"前端处理了契约外的事件: {sorted(stray)} → 要么是野事件（删掉 case），要么在 protocol.py 登记"


def test_frontend_covers_every_declared_event_at_most_once():
    """每个事件在分发表里至多一个 case（双 case 会掩盖优先级 bug）。"""
    src = _APP_JS.read_text(encoding="utf-8")
    start = src.index("function handleEvent")
    depth, end = 0, start
    open_brace = src.index("{", start)
    for i in range(open_brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    body = src[start:end]
    found = re.findall(r"case\s+'([A-Za-z0-9_]+)'", body)
    dupes = {n for n in found if found.count(n) > 1}
    assert not dupes, f"handleEvent 里重复 case: {dupes}"
