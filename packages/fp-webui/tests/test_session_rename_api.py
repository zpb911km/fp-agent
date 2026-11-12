"""webui 会话重命名 API（POST /api/sessions/{sid}/rename）

契约：语义在核内（``SessionCtl.rename`` → ``SessionManager.set_summary``），
本层只做 HTTP 契约：body 校验、404（会话不存在）、以及把生效文本回给前端。
``summary_limit`` 由 ``GET /api/sessions`` 下发（前端 maxlength 只读它）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from fp_core.core.session import SUMMARY_LIMIT
from fp_webui import main


def _patch(monkeypatch, *, result="新名字", current="s_cur", sessions=None):
    """桩化 portal.ctl.sessions.rename/summary_limit/list + ensure_agent。"""
    calls: list[tuple[str, str]] = []

    class _Sessions:
        def rename(self, sid: str, summary: str):
            calls.append((sid, summary))
            if result == "MISSING":
                return None
            return result

        def summary_limit(self) -> int:
            return SUMMARY_LIMIT

        def list(self):
            return sessions or {}

    fake = SimpleNamespace(
        ctl=SimpleNamespace(sessions=_Sessions()),
        run=SimpleNamespace(status=SimpleNamespace(session_id=current)),
    )
    monkeypatch.setattr(main, "portal", fake)

    async def _noop() -> None:
        return None

    monkeypatch.setattr(main, "ensure_agent", _noop)
    return calls


def test_rename_returns_effective_summary(monkeypatch):
    """正常重命名 → 回生效文本（截断后的），带 renamed 标记"""
    calls = _patch(monkeypatch, result="重构 会话模块")
    out = asyncio.run(main.rename_session("s_other", {"summary": "  重构 会话模块  "}))
    assert out == {
        "status": "renamed",
        "session_id": "s_other",
        "summary": "重构 会话模块",
        "renamed": True,
    }
    assert calls == [("s_other", "  重构 会话模块  ")]  # 规范化交给核内


def test_rename_empty_revokes(monkeypatch):
    """空串 = 撤销自定义名 → renamed False（回到自动摘要）"""
    _patch(monkeypatch, result="自动派生的名字")
    out = asyncio.run(main.rename_session("s_cur", {"summary": ""}))
    assert out["summary"] == "自动派生的名字"
    assert out["renamed"] is False


def test_rename_missing_body_field(monkeypatch):
    """body 缺 summary → 400（不静默当成撤销）"""
    _patch(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(main.rename_session("s_cur", {}))
    assert ei.value.status_code == 400


def test_rename_non_string_summary(monkeypatch):
    """summary 非字符串 → 400"""
    _patch(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(main.rename_session("s_cur", {"summary": 123}))
    assert ei.value.status_code == 400


def test_rename_missing_session(monkeypatch):
    """核内返回 None（会话不存在）→ 404"""
    _patch(monkeypatch, result="MISSING")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(main.rename_session("s_ghost", {"summary": "x"}))
    assert ei.value.status_code == 404


def test_list_sessions_carries_summary_limit(monkeypatch):
    """列表端点下发 summary_limit（前端 maxlength 的唯一来源）"""
    monkeypatch.setattr(
        main,
        "portal",
        SimpleNamespace(
            ctl=SimpleNamespace(sessions=SimpleNamespace(list=lambda: {}, summary_limit=lambda: SUMMARY_LIMIT)),
            run=SimpleNamespace(status=SimpleNamespace(session_id="s_cur")),
        ),
    )

    async def _noop() -> None:
        return None

    monkeypatch.setattr(main, "ensure_agent", _noop)

    out = asyncio.run(main.list_sessions())
    assert out["summary_limit"] == SUMMARY_LIMIT
