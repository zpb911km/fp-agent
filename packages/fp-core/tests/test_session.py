"""测试 SessionManager — 会话持久化，临时目录隔离"""

import json
import os
from unittest.mock import patch

import pytest

import fp_core.config as cfg
import fp_core.core.session as session_mod
from fp_core.core.session import SessionManager, _extract_sid, _generate_sid, _is_session_file


@pytest.fixture
def sessions_dir(tmp_path):
    """创建临时会话目录并 patch 所有引用点。"""
    d = str(tmp_path / "sessions")
    os.makedirs(d, exist_ok=True)

    with patch.object(cfg, "SESSIONS_DIR", d), patch.object(session_mod, "SESSIONS_DIR", d):
        yield d


class TestSessionHelpers:
    """会话辅助函数"""

    def test_generate_sid_format(self):
        """_generate_sid() 返回 s_ 开头、含微秒的 sid"""
        sid = _generate_sid()
        assert sid.startswith("s_")
        assert len(sid) > 15

    def test_generate_sid_unique(self):
        """连续生成的 sid 不相同"""
        sids = {_generate_sid() for _ in range(10)}
        assert len(sids) == 10  # 全部唯一

    def test_is_session_file_valid(self):
        """合法会话文件名 → True"""
        assert _is_session_file("s_260606_160012345678.jsonl") is True

    def test_is_session_file_invalid(self):
        """非法文件名 → False"""
        assert _is_session_file("notes.txt") is False
        assert _is_session_file("s_short.jsonl") is False
        assert _is_session_file("random_file.jsonl") is False

    def test_extract_sid(self):
        """_extract_sid() 从文件名提取 sid"""
        sid = _extract_sid("s_260606_160012345678.jsonl")
        assert sid == "s_260606_160012345678"

    def test_extract_sid_with_summary(self):
        """_extract_sid() 处理带 summary 后缀的文件"""
        sid = _extract_sid("s_260606_160012345678_summary_test.jsonl")
        assert sid == "s_260606_160012345678"


class TestSessionManager:
    """SessionManager — 会话生命周期"""

    def test_create_session(self, sessions_dir):
        """创建会话 → 分配 sid，但不立即创建文件（惰性创建）"""
        sm = SessionManager(resume=False)
        sid = sm.session_id
        assert sid.startswith("s_")

        # 惰性创建：文件尚不存在
        path = sm.get_session_path()
        assert os.path.exists(path) is False

    def test_save_message_creates_file(self, sessions_dir):
        """save_message() 首次调用时自动创建文件"""
        sm = SessionManager(resume=False)
        sm.save_message("user", "你好")

        path = sm.get_session_path()
        assert os.path.exists(path) is True

    def test_save_and_load_context(self, sessions_dir):
        """写入消息后能正确加载回来（load_context 不含 system prompt）"""
        sm = SessionManager(resume=False)
        sm.save_message("user", "第一条消息")
        sm.save_message("assistant", "回复")

        context = sm.load_context("你是一个助手")
        assert len(context) == 2  # user + assistant（无 system）
        assert context[0]["role"] == "user"
        assert context[0]["content"] == "第一条消息"
        assert context[1]["role"] == "assistant"
        assert context[1]["content"] == "回复"

    def test_save_context_rewrites_file(self, sessions_dir):
        """save_context() 重写整个文件而非追加"""
        sm = SessionManager(resume=False)
        sm.save_message("user", "旧消息")

        # 用 save_context 重写
        sm.save_context([
            {"role": "user", "content": "新消息"},
            {"role": "assistant", "content": "新回复"},
        ])

        context = sm.load_context("system prompt")
        assert len(context) == 2  # user + assistant（无 system）
        assert context[0]["content"] == "新消息"

    def test_list_sessions(self, sessions_dir):
        """list_sessions() 列出所有会话"""
        sm1 = SessionManager(resume=False)
        sm1.save_message("user", "会话1")
        sid1 = sm1.session_id

        sm2 = SessionManager(resume=False)
        sm2.save_message("user", "会话2")
        sid2 = sm2.session_id

        sessions = sm1.list_sessions()
        assert sid1 in sessions
        assert sid2 in sessions
        assert len(sessions) == 2

    def test_switch_session(self, sessions_dir):
        """switch_session() 切换后能读取目标会话的消息"""
        sm = SessionManager(resume=False)
        sm.save_message("user", "在原始会话中")
        original_sid = sm.session_id

        # 创建并切换到第二个会话
        sm.create_session()
        sm.save_message("user", "在新会话中")

        # 切回原始会话
        result = sm.switch_session(original_sid)
        assert result is True
        context = sm.load_context("")
        assert any("在原始会话中" in m.get("content", "") for m in context)
        assert not any("在新会话中" in m.get("content", "") for m in context)

    def test_switch_nonexistent_session(self, sessions_dir):
        """切换到不存在的会话 → 返回 False"""
        sm = SessionManager(resume=False)
        result = sm.switch_session("s_999999_999999999999")
        assert result is False

    def test_delete_session(self, sessions_dir):
        """delete_session() 删除非当前会话"""
        sm = SessionManager(resume=False)
        # 创建并切换到会话A
        sm.create_session()
        sm.save_message("user", "会话A")
        sid_a = sm.session_id

        # 再创建并切换到会话B（此时 A 不是当前会话）
        sm.create_session()
        sm.save_message("user", "会话B")

        # 可以删除非当前会话 A
        result = sm.delete_session(sid_a)
        assert result is True
        assert sid_a not in sm.list_sessions()

    def test_cannot_delete_current_session(self, sessions_dir):
        """不能删除当前会话"""
        sm = SessionManager(resume=False)
        result = sm.delete_session(sm.session_id)
        assert result is False

    def test_clear_session_file(self, sessions_dir):
        """clear_session_file() 清空消息但保留 meta"""
        sm = SessionManager(resume=False)
        sm.save_message("user", "将被清空")
        sm.save_message("assistant", "也将被清空")

        sm.clear_session_file()
        context = sm.load_context("")
        assert len(context) == 0  # 消息已清空（无 system prompt）

    def test_resume_latest(self, sessions_dir):
        """resume_latest() 续最近会话"""
        # 先创建一个会话并写入消息
        sm1 = SessionManager(resume=False)
        sm1.save_message("user", "旧会话的消息")
        sid_old = sm1.session_id

        # 新的 SessionManager 续最近会话
        sm2 = SessionManager(resume=True)
        assert sm2.session_id == sid_old
        context = sm2.load_context("")
        assert any("旧会话的消息" in m.get("content", "") for m in context)

    def test_resume_specific_session(self, sessions_dir):
        """resume='s_xxx' 续指定会话"""
        sm1 = SessionManager(resume=False)
        sm1.save_message("user", "特定会话")
        target_sid = sm1.session_id

        sm2 = SessionManager(resume=target_sid)
        assert sm2.session_id == target_sid

    def test_meta_tracks_message_count(self, sessions_dir):
        """meta 中的 message_count 随消息追加更新"""
        sm = SessionManager(resume=False)
        assert sm._meta["message_count"] == 0

        sm.save_message("user", "消息1")
        assert sm._meta["message_count"] >= 1

        sm.save_message("assistant", "消息2")
        assert sm._meta["message_count"] >= 2

    def test_empty_directory_list(self, sessions_dir):
        """空目录下列表只含当前会话的内存 meta，且不为此写文件"""
        sm = SessionManager(resume=False)
        sessions = sm.list_sessions()

        # 当前会话（惰性 sid，盘上无文件）也必须可见——替代旧的占位文件方案
        assert set(sessions) == {sm.session_id}
        assert sessions[sm.session_id]["message_count"] == 0
        assert sessions[sm.session_id]["summary"] == session_mod.EMPTY_SESSION_LABEL
        # 关键：可见性不再靠写 0 长度会话文件换取
        assert os.path.exists(sm.get_session_path()) is False

    def test_load_context_from_empty_file(self, sessions_dir):
        """空文件（无会话）的 load_context 返回空列表"""
        sm = SessionManager(resume=False)
        context = sm.load_context("测试 system prompt")
        assert len(context) == 0

    def test_update_meta(self, sessions_dir):
        """update_meta() 修改后可从文件重新读取"""
        sm = SessionManager(resume=False)
        sm.save_message("user", "test")
        sm.update_meta(summary="测试摘要")

        # 新实例读取 meta
        sm2 = SessionManager(resume=sm.session_id)
        assert sm2._meta.get("summary") == "测试摘要"


class TestCurrentSessionRegistration:
    """switch_session / create_session 必须同步进程级 `_current_session_id`

    `get_current_session_id()`（session.py）供插件（如 journal）给事件标注会话归属。
    它原先只在 SessionManager.__init__ 注册，切换会话后停留在旧值 → 归属写错。
    """

    def test_switch_session_updates_current_sid(self, sessions_dir):
        from fp_core.core.session import get_current_session_id

        mgr = SessionManager(resume=False)
        first = mgr.session_id
        mgr.save_context([{"role": "user", "content": "A"}])
        assert get_current_session_id() == first

        second = mgr.create_session()
        assert get_current_session_id() == second, "create_session 未同步进程级 current_sid"

        assert mgr.switch_session(first) is True
        assert get_current_session_id() == first, "switch_session 未同步进程级 current_sid"

    def test_failed_switch_keeps_current_sid(self, sessions_dir):
        from fp_core.core.session import get_current_session_id

        mgr = SessionManager(resume=False)
        before = get_current_session_id()
        assert mgr.switch_session("s_260101_000000000000") is False  # 不存在
        assert get_current_session_id() == before


def _first_line_meta(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.loads(f.readline())


def _rewrite_first_line(path: str, meta: dict) -> None:
    with open(path, encoding="utf-8") as f:
        rest = f.readlines()[1:]
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        f.writelines(rest)


class TestZeroLengthSessionHygiene:
    """0 长度会话不再凭空产生 + 列表口径（隐藏占位 / 摘要回填）

    背景：会话列表长期被「0 消息占位文件」和「摘要为空只能显示 sid」
    两类记录污染。本组测试锁住三道防线：
      1. 空落盘 / 清空 / 标记类写入一律**不创建**会话文件；
      2. list_sessions() 隐藏所有 0 长度会话（当前会话除外）、
         回填缺失摘要、合并当前内存会话；
      3. auto-resume 不落到空占位上。
    """

    def test_save_and_summarize_empty_does_not_create_file(self, sessions_dir):
        """空会话落盘 → 完全 no-op，不产生 0 长度会话文件"""
        sm = SessionManager(resume=False)
        assert sm.save_and_summarize([]) == ""
        assert os.path.exists(sm.get_session_path()) is False

    def test_save_and_summarize_keeps_existing_summary(self, sessions_dir):
        """算不出新摘要时不抹掉盘上旧摘要（此前会被写成空串/empty_session）"""
        sm = SessionManager(resume=False)
        sm.save_message("user", "这是一个有意义的问题")
        sid = sm.session_id

        assert sm.save_and_summarize([], sid) == "这是一个有意义的问题"
        assert _first_line_meta(sm.get_session_path())["summary"] == "这是一个有意义的问题"

    def test_clear_session_file_without_file_is_noop(self, sessions_dir):
        """clear_session_file() 对不存在的文件不落盘（/new、/clear 的占位来源）"""
        sm = SessionManager(resume=False)
        sid = sm.session_id

        sm.clear_session_file()

        assert os.path.exists(sm.get_session_path()) is False
        assert sm.session_id == sid

    def test_update_meta_create_false_stays_in_memory(self, sessions_dir):
        """create=False 的标记类写入只改内存，随首条消息一起落盘"""
        sm = SessionManager(resume=False)

        sm.update_meta(source="subagent", create=False)
        assert os.path.exists(sm.get_session_path()) is False
        assert sm.meta["source"] == "subagent"

        sm.save_message("user", "子任务内容")
        assert _first_line_meta(sm.get_session_path())["source"] == "subagent"

    def test_save_message_sets_summary_eagerly(self, sessions_dir):
        """user 消息一落盘就带摘要（进程被杀也不丢）"""
        sm = SessionManager(resume=False)
        sm.save_message("user", "帮我定位这个 bug")
        assert sm.meta["summary"] == "帮我定位这个 bug"

    def test_list_hides_infoless_placeholder(self, sessions_dir):
        """无消息 + 无摘要 + 非当前 → 不出现在列表"""
        session_mod.update_session_meta("s_260101_000000000001")  # 造一个占位文件
        sm = SessionManager(resume=False)

        sessions = sm.list_sessions()
        assert "s_260101_000000000001" not in sessions
        assert sm.session_id in sessions  # 当前会话仍可见

    def test_list_hides_zero_length_even_with_summary(self, sessions_dir):
        """0 长度会话即使带摘要也不进列表（列表只放能续接的对话）"""
        session_mod.update_session_meta("s_260101_000000000002", source="subagent", summary="[subagent] 读取配置")
        sm = SessionManager(resume=False)

        assert "s_260101_000000000002" not in sm.list_sessions()
        # 隐藏 ≠ 删除：文件原样留在盘上，取证/恢复不受影响
        assert os.path.exists(os.path.join(sessions_dir, "s_260101_000000000002.jsonl"))

    def test_list_derives_and_heals_missing_summary(self, sessions_dir):
        """meta.summary 缺失的老会话 → 从文件回填，并写回（不碰 updated）"""
        sm = SessionManager(resume=False)
        sm.save_message("user", "帮我看看这段代码为什么报错")
        sid = sm.session_id

        path = sm.get_session_path()
        before = _first_line_meta(path)
        before["summary"] = ""  # 模拟从未走过 save_and_summarize 的老会话
        _rewrite_first_line(path, before)

        sessions = sm.list_sessions()
        assert sessions[sid]["summary"] == "帮我看看这段代码为什么报错"
        assert _first_line_meta(path)["summary"] == "帮我看看这段代码为什么报错"  # 已自愈
        assert _first_line_meta(path)["updated"] == before["updated"]  # 排序不受影响

    def test_list_labels_empty_and_summaryless(self, sessions_dir):
        """空会话给「(空白会话)」、有消息但摘不出来给「(无摘要)」——不显示 sid"""
        sm = SessionManager(resume=False)
        sm.create_session()
        empty_sid = sm.session_id
        sm.clear_session_file()
        assert sm.list_sessions()[empty_sid]["summary"] == session_mod.EMPTY_SESSION_LABEL

        # 有消息但没有任何 user 消息 → (无摘要)
        other = SessionManager(resume=False)
        other.save_message("assistant", "只有模型自言自语")
        shown = sm.list_sessions()[other.session_id]["summary"]
        assert shown == session_mod.NO_SUMMARY_LABEL

    def test_resume_latest_prefers_non_empty(self, sessions_dir):
        """auto-resume 落到有消息的会话，而不是 updated 更新的空占位"""
        sm1 = SessionManager(resume=False)
        sm1.save_message("user", "有效历史")
        used_sid = sm1.session_id

        placeholder = _generate_sid()
        session_mod.update_session_meta(placeholder)  # 空占位，时间更新

        sm2 = SessionManager(resume=True)
        assert sm2.session_id == used_sid

    def test_prune_moves_only_message_less_files(self, sessions_dir):
        """prune 只搬「正文无任何消息行」的文件；有效历史与当前会话不碰"""
        sm = SessionManager(resume=False)
        sm.save_message("user", "当前会话的有效历史")
        current_sid = sm.session_id

        session_mod.update_session_meta("s_260101_000000000003")  # 空占位 → 该清
        session_mod.update_session_meta(
            "s_260101_000000000004", source="subagent", summary="[subagent] 任务文本"
        )  # 0 长度但有摘要 → 同样搬（摘要随文件进 .trash/，可搬回）

        # 当前会话恰好是个空占位（文件在盘上）→ 必须豁免
        live = session_mod._generate_sid()  # pyright: ignore[reportPrivateUsage]
        session_mod.update_session_meta(live)
        holder = SessionManager(resume=live)

        # 有消息但 meta.summary 为空的老会话 → 保留（有消息 = 有效历史）
        other = SessionManager(resume=False)
        other.save_message("user", "另一段有效历史")
        other_sid = other.session_id
        other_path = other.get_session_path()
        stale = _first_line_meta(other_path)
        stale["summary"] = ""
        _rewrite_first_line(other_path, stale)

        moved = holder.prune_empty_sessions()

        assert moved == ["s_260101_000000000003", "s_260101_000000000004"]
        trash = session_mod.session_trash_dir()
        for sid in ("s_260101_000000000003", "s_260101_000000000004"):
            assert os.path.exists(os.path.join(trash, sid + ".jsonl"))
            assert not os.path.exists(os.path.join(sessions_dir, sid + ".jsonl"))
        assert os.path.exists(os.path.join(sessions_dir, live + ".jsonl"))  # 当前会话
        assert os.path.exists(other_path)  # 有效历史
        assert os.path.exists(sm.get_session_path())  # 有效历史
        # 被清走的会话从列表里彻底消失；有效会话仍在
        sessions = holder.list_sessions()
        assert "s_260101_000000000003" not in sessions
        assert other_sid in sessions and current_sid in sessions
