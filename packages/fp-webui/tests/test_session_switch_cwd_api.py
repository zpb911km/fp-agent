"""webui 会话切换 API 的 cwd 契约（POST /api/sessions/{sid}/switch）

背景：cwd 是**会话属性**（`SessionManager._bind_session_cwd`）。前端切会话时，
相对路径基准跟着目标会话走 —— 跨项目切换会让用户后续的文件操作整条换目录。
本端点的职责是把「变了没有」如实告诉前端（`cwd` / `cwd_changed`），由 UI 提示；
否则就是静默跳目录：文件写进另一个项目，不报错、不写日志。

只测 HTTP 契约：核内返回的 `SessionInfo.cwd/previous_cwd` 如何折算成响应字段。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from fp_core.core.session_ops import SessionInfo
from fp_webui import main


def _patch(monkeypatch, *, info: SessionInfo | None):
    """桩化 portal.ctl.sessions.load + ensure_agent。"""

    class _Sessions:
        def load(self, sid: str):
            return info

    monkeypatch.setattr(
        main,
        "portal",
        SimpleNamespace(
            ctl=SimpleNamespace(sessions=_Sessions()),
            run=SimpleNamespace(status=SimpleNamespace(session_id="s_cur")),
        ),
    )

    async def _noop() -> None:
        return None

    monkeypatch.setattr(main, "ensure_agent", _noop)


def test_switch_reports_cwd_change(monkeypatch):
    """跨项目切换 → cwd_changed=True，且带上目标会话的 cwd（前端据此提示）"""
    _patch(
        monkeypatch,
        info=SessionInfo(
            session_id="s_a",
            previous_sid="s_b",
            cwd="/proj/A",
            previous_cwd="/proj/B",
        ),
    )
    out = asyncio.run(main.switch_session_endpoint("s_a"))
    assert out == {
        "session_id": "s_a",
        "status": "switched",
        "cwd": "/proj/A",
        "cwd_changed": True,
    }


def test_switch_same_cwd_is_quiet(monkeypatch):
    """同一项目内切换 → cwd_changed=False（不打扰用户）"""
    _patch(
        monkeypatch,
        info=SessionInfo(session_id="s_a", previous_sid="s_b", cwd="/proj/A", previous_cwd="/proj/A"),
    )
    out = asyncio.run(main.switch_session_endpoint("s_a"))
    assert out["cwd"] == "/proj/A"
    assert out["cwd_changed"] is False


def test_switch_without_known_cwd_is_quiet(monkeypatch):
    """老会话没有 meta.cwd（改造前创建）→ cwd 空串，不得谎报「目录变了」"""
    _patch(monkeypatch, info=SessionInfo(session_id="s_old", previous_sid="s_b", previous_cwd="/proj/B"))
    out = asyncio.run(main.switch_session_endpoint("s_old"))
    assert out["cwd"] == ""
    assert out["cwd_changed"] is False


def test_switch_missing_session(monkeypatch):
    """会话不存在 → 404（与前端「点了个已删除会话」对齐）"""
    _patch(monkeypatch, info=None)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(main.switch_session_endpoint("s_ghost"))
    assert ei.value.status_code == 404
