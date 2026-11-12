"""webui 会话关键词重算 API（POST /api/sessions/refresh 与 /{sid}/refresh）

契约：同步返回（core KL 管线纯本地零 LLM、单会话约 18ms，无需异步）；
复用 fp-core 模块级 session_keywords.update_session（算并写回 meta.kw）。
重算实现本身在 fp-core/tests（KL 管线单测），此处只验 webui 端点契约。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from fp_core.core import session_keywords
from fp_webui import main


def _patch(monkeypatch, tmp_path: Path, *, sid: str, current: str, kws):
    """桩化 portal（path/当前会话）+ ensure_agent + update_session。"""
    session_file = tmp_path / f"{sid}.jsonl"
    session_file.write_text('{"__meta__": true}\n', encoding="utf-8")

    class _Sessions:
        def path(self, s: str) -> str:
            return str(session_file)

        def refresh_keywords(self, s: str):
            # 正门方法在 fp-core api 层实现；此处转给被 patch 的 update_session，
            # 使「端点 → 正门 → 核内算法」这条缝在端点测试里仍可断言
            return session_keywords.update_session(s, str(session_file)) or []

    calls: list[tuple[str, str]] = []

    def _fake_update(s: str, path: str, sessions_dir=None):
        calls.append((s, path))
        return kws

    fake = SimpleNamespace(
        ctl=SimpleNamespace(sessions=_Sessions()),
        run=SimpleNamespace(status=SimpleNamespace(session_id=current)),
    )
    monkeypatch.setattr(main, "portal", fake)
    monkeypatch.setattr(session_keywords, "update_session", _fake_update)

    async def _noop() -> None:
        return None

    monkeypatch.setattr(main, "ensure_agent", _noop)
    return session_file, calls


def test_refresh_current_session(monkeypatch, tmp_path):
    """无 sid 参数 → 重算当前会话（刷新按钮主路径）"""
    _, calls = _patch(monkeypatch, tmp_path, sid="s_cur", current="s_cur", kws=["新词"])
    out = asyncio.run(main.refresh_current_keywords())
    assert out == {"status": "refreshed", "sid": "s_cur", "kw": ["新词"]}
    assert calls and calls[0][0] == "s_cur"


def test_refresh_specific_session(monkeypatch, tmp_path):
    """带 sid → 重算指定会话（单卡「⟳ 重算 kw」入口）"""
    _, calls = _patch(monkeypatch, tmp_path, sid="s_other", current="s_cur", kws=["词A", "词B"])
    out = asyncio.run(main.refresh_session_keywords("s_other"))
    assert out["sid"] == "s_other"
    assert out["kw"] == ["词A", "词B"]
    assert calls == [("s_other", calls[0][1])]


def test_refresh_404_when_file_missing(monkeypatch, tmp_path):
    """会话文件不存在（新会话惰性未落盘）→ 404，前端降级为仅刷列表"""
    _patch(monkeypatch, tmp_path, sid="s_x", current="s_cur", kws=[])
    monkeypatch.setattr(main.portal.ctl.sessions, "path", lambda s: str(tmp_path / "nope.jsonl"))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(main.refresh_session_keywords("s_x"))
    assert ei.value.status_code == 404


def test_refresh_none_kw_returns_empty_list(monkeypatch, tmp_path):
    """update_session 静默失败/空会话（返回 None）→ kw 收敛为空列表，不抛"""
    _patch(monkeypatch, tmp_path, sid="s_cur", current="s_cur", kws=None)
    out = asyncio.run(main.refresh_current_keywords())
    assert out["status"] == "refreshed"
    assert out["kw"] == []


def test_list_sessions_exposes_updated(monkeypatch):
    """列表端点补 updated 字段（菜单 meta 层级依赖）"""
    info = {
        "s_1": {
            "message_count": 3,
            "created": "2026-11-01 10:00:00",
            "updated": "2026-11-02 11:00:00",
            "summary": "摘要",
            "kw": ["词"],
        }
    }
    fake = SimpleNamespace(
        ctl=SimpleNamespace(sessions=SimpleNamespace(list=lambda: info)),
        run=SimpleNamespace(status=SimpleNamespace(session_id="s_1")),
    )
    monkeypatch.setattr(main, "portal", fake)

    async def _noop() -> None:
        return None

    monkeypatch.setattr(main, "ensure_agent", _noop)
    out = asyncio.run(main.list_sessions())
    item = out["sessions"][0]
    assert item["created"] == "2026-11-01 10:00:00"
    assert item["updated"] == "2026-11-02 11:00:00"
    assert item["kw"] == ["词"]


def test_list_sessions_exposes_kw_show(monkeypatch):
    """列表端点下发 kw_show（主卡展示的关键词个数）

    单一真相源 = fp_core.core.session_keywords.TOP_SHOW（经 fp_core.api.KW_SHOW）。
    前端曾写死 3，而后端已是 5 —— 终端显示 5 个、webui 只显示 3 个。下发后前端只读。
    """
    fake = SimpleNamespace(
        ctl=SimpleNamespace(sessions=SimpleNamespace(list=lambda: {})),
        run=SimpleNamespace(status=SimpleNamespace(session_id="s_1")),
    )
    monkeypatch.setattr(main, "portal", fake)

    async def _noop() -> None:
        return None

    monkeypatch.setattr(main, "ensure_agent", _noop)
    out = asyncio.run(main.list_sessions())
    assert out["kw_show"] == session_keywords.TOP_SHOW


def test_search_sessions_exposes_kw_show(monkeypatch):
    """搜索端点同样下发 kw_show（前端两条路径共用一个渲染函数）"""
    fake = SimpleNamespace(
        ctl=SimpleNamespace(sessions=SimpleNamespace(search=lambda q, limit: [])),
        run=SimpleNamespace(status=SimpleNamespace(session_id="s_1")),
    )
    monkeypatch.setattr(main, "portal", fake)

    async def _noop() -> None:
        return None

    monkeypatch.setattr(main, "ensure_agent", _noop)
    out = asyncio.run(main.search_sessions(q="x"))
    assert out["kw_show"] == session_keywords.TOP_SHOW
