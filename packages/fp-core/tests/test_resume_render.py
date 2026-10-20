"""测试 /resume 列表渲染 — 关键词展示与筛选（纯渲染，不含切换逻辑）"""

from __future__ import annotations

from fp_core.core.session_view import kw_suffix, session_list_text


class _FakeSession:
    def __init__(self, sessions: dict):
        self._sessions = sessions

    def list_sessions(self) -> dict:
        return self._sessions


class _FakeState:
    def __init__(self, sessions: dict, current: str = "s_261020_100000000001"):
        self.session = _FakeSession(sessions)
        self.session_id = current


def _meta(summary: str, kw: list[str] | None, updated: str, **extra):
    m = {"summary": summary, "kw": kw or [], "updated": updated, "message_count": 3}
    m.update(extra)
    return m


SESSIONS = {
    "s_261020_100000000001": _meta("推送", ["定时器", "节流", "重排"], "2026-10-20 10:00:00"),
    "s_261020_100000000002": _meta("提交", ["量子退火", "排程"], "2026-10-19 10:00:00"),
    "s_261020_100000000003": _meta("ok", ["西红柿", "炒蛋"], "2026-10-18 10:00:00", source="subagent"),
}


class TestRender:
    def test_keywords_shown(self):
        text = session_list_text(_FakeState(SESSIONS))
        assert "定时器 节流 重排" in text
        assert "量子退火 排程" in text

    def test_meta_line_intact(self):
        text = session_list_text(_FakeState(SESSIONS))
        assert "3条" in text and "s_261020_100000000001" in text

    def test_current_marked(self):
        text = session_list_text(_FakeState(SESSIONS))
        assert "⬅" in text

    def test_subagent_tagged_in_full_list(self):
        text = session_list_text(_FakeState(SESSIONS))
        assert "🔹" in text

    def test_main_only_hides_subagent_but_keeps_numbering(self):
        main = session_list_text(_FakeState(SESSIONS), main_only=True)
        assert "西红柿" not in main  # subagent 被隐藏
        assert "**[3]**" not in main  # 原序号 [3] 消失
        assert "**[1]**" in main and "**[2]**" in main  # 其余序号不重排

    def test_missing_kw_renders_without_suffix(self):
        sessions = {"s_x": _meta("只有标题", None, "2026-10-20 10:00:00")}
        text = session_list_text(_FakeState(sessions, current="other"))
        assert "只有标题" in text
        assert " · " not in text.split("只有标题")[1].split("(")[0]

    def test_empty_kw_list_tolerated(self):
        sessions = {"s_x": {"summary": "标题", "kw": [], "updated": "2026-10-20 10:00:00", "message_count": 1}}
        assert "标题" in session_list_text(_FakeState(sessions, current="other"))

    def test_non_list_kw_tolerated(self):
        """脏数据（kw 是字符串）不能让列表崩掉。"""
        sessions = {"s_x": {"summary": "标题", "kw": "坏数据", "updated": "2026-10-20 10:00:00"}}
        assert "标题" in session_list_text(_FakeState(sessions, current="other"))


class TestFilter:
    def test_filter_by_keyword(self):
        text = session_list_text(_FakeState(SESSIONS), terms=["量子退火"])
        assert "量子退火" in text
        assert "西红柿" not in text
        assert "1/3" in text  # 命中计数

    def test_filter_by_summary(self):
        text = session_list_text(_FakeState(SESSIONS), terms=["推送"])
        assert "定时器" in text
        assert "量子退火" not in text

    def test_filter_by_sid_fragment(self):
        text = session_list_text(_FakeState(SESSIONS), terms=["000000003"])
        assert "西红柿" in text

    def test_filter_multi_term_and(self):
        text = session_list_text(_FakeState(SESSIONS), terms=["量子退火", "排程"])
        assert "量子退火" in text
        text2 = session_list_text(_FakeState(SESSIONS), terms=["量子退火", "西红柿"])
        assert "没有匹配" in text2

    def test_filter_no_match_message(self):
        text = session_list_text(_FakeState(SESSIONS), terms=["不存在的词"])
        assert "没有匹配" in text

    def test_filter_keeps_original_index(self):
        """筛选不重编号——`/resume 2` 在任何视图下都指向同一会话。"""
        text = session_list_text(_FakeState(SESSIONS), terms=["量子退火"])
        assert "**[2]**" in text

    def test_filter_case_insensitive(self):
        sessions = {"s_x": _meta("Fix Parser", ["Parser", "Bugfix"], "2026-10-20 10:00:00")}
        text = session_list_text(_FakeState(sessions, current="other"), terms=["parser"])
        assert "Fix Parser" in text


class TestKwSuffixUnit:
    def test_limits_to_three(self):
        assert kw_suffix({"kw": ["一", "二", "三", "四"]}) == " · 一 二 三"

    def test_empty_cases(self):
        assert kw_suffix({}) == ""
        assert kw_suffix({"kw": []}) == ""
        assert kw_suffix({"kw": None}) == ""
