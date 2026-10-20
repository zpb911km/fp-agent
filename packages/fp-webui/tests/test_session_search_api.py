"""webui 会话正文搜索 API（/api/sessions/search）

只验接口契约与字段映射——检索实现本身在 fp-core/tests/test_session_search.py。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fp_webui import main


class _Sessions:
    def __init__(self, results):
        self._results = results
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, *, limit: int = 30):
        self.calls.append((query, limit))
        return self._results


def _patch(monkeypatch, results, current="s_current"):
    sessions = _Sessions(results)
    fake = SimpleNamespace(
        ctl=SimpleNamespace(sessions=sessions),
        run=SimpleNamespace(status=SimpleNamespace(session_id=current)),
    )
    monkeypatch.setattr(main, "portal", fake)

    async def _noop() -> None:
        return None

    monkeypatch.setattr(main, "ensure_agent", _noop)
    return sessions


def test_maps_meta_and_snippets(monkeypatch):
    sessions = _patch(
        monkeypatch,
        [
            {
                "id": "s_1",
                "meta": {
                    "summary": "退火排程",
                    "kw": ["量子", "退火"],
                    "message_count": 12,
                },
                "updated": "2026-10-20 10:00:00",
                "snippets": [{"role": "user", "text": "…量子退火…"}],
            }
        ],
    )

    out = asyncio.run(main.search_sessions("量子"))
    assert sessions.calls == [("量子", 30)]
    item = out["results"][0]
    assert item["id"] == "s_1"
    assert item["summary"] == "退火排程"
    assert item["kw"] == ["量子", "退火"]
    assert item["snippets"][0]["role"] == "user"
    assert item["is_current"] is False


def test_current_flag(monkeypatch):
    _patch(
        monkeypatch,
        [{"id": "s_current", "meta": {}, "updated": "", "snippets": []}],
        current="s_current",
    )
    out = asyncio.run(main.search_sessions("x"))
    assert out["results"][0]["is_current"] is True


def test_empty_query_passes_through(monkeypatch):
    """空查询交给 api 层判定（返回空结果，不做全库扫描）"""
    sessions = _patch(monkeypatch, [])
    out = asyncio.run(main.search_sessions(""))
    assert out["results"] == []
    assert sessions.calls == [("", 30)]
