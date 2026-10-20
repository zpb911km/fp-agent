"""会话正文检索 + /find 渲染 + /resume 降级搜索

关键设计断言：
- 只检索 user/assistant 正文，**tool 返回值一律不进检索范围**
  （tool 返回占语料 50%，不过滤会让搜任何词都命中一堆日志）
- 序号与 /resume list 共用排序池，搜到的序号可直接 /resume <n> 切换
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fp_core.commands import resume
from fp_core.core import session_search, session_view


def _write_session(directory, sid: str, updated: str, messages: list[dict], **meta_extra):
    """造一个会话文件（格式见 session.py 模块文档）"""
    meta = {
        "__meta__": True,
        "id": sid,
        "created": updated,
        "updated": updated,
        "summary": meta_extra.pop("summary", ""),
        "message_count": len(messages),
    }
    meta.update(meta_extra)
    path = directory / f"{sid}.jsonl"
    lines = [json.dumps(meta, ensure_ascii=False)]
    lines += [json.dumps(m, ensure_ascii=False) for m in messages]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    _write_session(
        tmp_path,
        "s_261020_100000000001",
        "2026-10-20 10:00:00",
        [
            {"role": "user", "content": "帮我看看量子退火的排程问题"},
            {"role": "assistant", "content": "排程用模拟退火更快，量子退火在这里不划算"},
            {"role": "tool", "content": "日志里出现了量子退火字样，但这是工具返回"},
        ],
        summary="退火排程",
        kw=["量子", "退火", "排程"],
    )
    _write_session(
        tmp_path,
        "s_261019_100000000002",
        "2026-10-19 10:00:00",
        [
            {"role": "user", "content": "西红柿炒蛋怎么做"},
            {"role": "assistant", "content": "先炒蛋再下西红柿"},
        ],
        summary="炒菜",
        kw=["西红柿", "炒蛋"],
    )
    _write_session(
        tmp_path,
        "s_261018_100000000003",
        "2026-10-18 10:00:00",
        [
            {"role": "user", "content": "随便聊聊"},
            {"role": "tool", "content": "量子 这个词只出现在工具返回里"},
        ],
        summary="闲聊",
        kw=[],
    )
    monkeypatch.setattr(session_search, "sessions_dir", lambda: tmp_path)
    return tmp_path


class TestSearchCore:
    def test_body_hit_found(self, corpus):
        results = session_search.search_sessions(["量子"], directory=corpus)
        assert [r["sid"] for r in results] == ["s_261020_100000000001"]

    def test_tool_output_excluded(self, corpus):
        """tool 返回命中的会话不得出现在结果里（不过滤会淹掉真正的对话）"""
        sids = [r["sid"] for r in session_search.search_sessions(["量子"], directory=corpus)]
        assert "s_261018_100000000003" not in sids

    def test_multi_term_is_and(self, corpus):
        assert session_search.search_sessions(["量子", "退火"], directory=corpus)
        assert not session_search.search_sessions(["量子", "西红柿"], directory=corpus)

    def test_snippet_has_context(self, corpus):
        results = session_search.search_sessions(["量子"], directory=corpus)
        text = results[0]["snippets"][0]["text"]
        assert "量子" in text

    def test_empty_query(self, corpus):
        assert session_search.search_sessions([], directory=corpus) == []
        assert session_search.search_sessions(["  "], directory=corpus) == []

    def test_no_match(self, corpus):
        assert session_search.search_sessions(["不存在的词xyz"], directory=corpus) == []

    def test_sorted_by_updated_desc(self, corpus):
        _write_session(
            corpus,
            "s_261017_100000000004",
            "2026-10-17 10:00:00",
            [{"role": "user", "content": "再谈量子"}],
        )
        results = session_search.search_sessions(["量子"], directory=corpus)
        assert [r["sid"] for r in results] == [
            "s_261020_100000000001",
            "s_261017_100000000004",
        ]


class _FakeSession:
    def __init__(self, sessions: dict):
        self._sessions = sessions

    def list_sessions(self) -> dict:
        return self._sessions


class _FakeState:
    def __init__(self, sessions: dict, current: str = ""):
        self.session = _FakeSession(sessions)
        self.session_id = current


def _state_from(corpus) -> _FakeState:
    sessions = {}
    for path in corpus.glob("*.jsonl"):
        sessions[path.stem] = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    return _FakeState(sessions)


class TestFindRender:
    def test_shows_snippet_and_index(self, corpus):
        text = session_view.render_search(_state_from(corpus), ["量子"])
        assert "命中" in text
        assert "量子" in text
        # 正文命中 1 个（tool-only 会话被排除）
        assert "正文 1" in text

    def test_metadata_only_hit_listed(self, corpus):
        """正文没提「西红柿」以外的词时，仅标题/关键词命中也要列出来"""
        state = _state_from(corpus)
        text = session_view.render_search(state, ["炒菜"])
        assert "仅标题/关键词 1" in text

    def test_index_matches_resume_pool(self, corpus):
        """序号必须与 /resume list 完全一致，否则跳转会切错会话"""
        from fp_core.core.session_view import sorted_sessions

        state = _state_from(corpus)
        pool = sorted_sessions(state)
        expected = {sid: i for i, (sid, _) in enumerate(pool, 1)}
        text = session_view.render_search(state, ["量子"])
        assert f"[{expected['s_261020_100000000001']}]" in text

    def test_empty_arg_usage(self, corpus):
        assert "用法" in session_view.render_search(_state_from(corpus), [])

    def test_no_match_message(self, corpus):
        assert "没有找到" in session_view.render_search(_state_from(corpus), ["不存在词xyz"])


class TestResumeFallback:
    def test_unknown_word_falls_back_to_search(self, corpus):
        """'/resume 量子' 不是 sid/序号 → 自动按正文搜索，而不是报「会话不存在」"""
        state = _state_from(corpus)
        ok, out = asyncio.run(resume.execute(state, "量子"))
        assert ok and "命中" in out and "量子" in out

    def test_out_of_range_number_still_errors(self, corpus):
        state = _state_from(corpus)
        ok, out = asyncio.run(resume.execute(state, "9999"))
        assert "超出范围" in out

    def test_list_filter_unaffected(self, corpus):
        """带 'list' 前缀仍走元数据筛选，不被降级逻辑抢走"""
        state = _state_from(corpus)
        ok, out = asyncio.run(resume.execute(state, "list 量子"))
        assert "筛选" in out
