"""测试 SessionManager — 会话持久化，临时目录隔离"""

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
        """写入消息后能正确加载回来"""
        sm = SessionManager(resume=False)
        sm.save_message("user", "第一条消息")
        sm.save_message("assistant", "回复")

        context = sm.load_context("你是一个助手")
        assert len(context) == 3  # system + user + assistant
        assert context[1]["role"] == "user"
        assert context[1]["content"] == "第一条消息"
        assert context[2]["role"] == "assistant"
        assert context[2]["content"] == "回复"

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
        assert len(context) == 3  # system + user + assistant
        assert context[1]["content"] == "新消息"

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
        assert len(context) == 1  # 只有 system

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
        """空目录的 list_sessions() 返回空 dict"""
        sm = SessionManager(resume=False)
        sessions = sm.list_sessions()
        assert sessions == {}

    def test_load_context_from_empty_file(self, sessions_dir):
        """空文件（无会话）的 load_context 只返回 system prompt"""
        sm = SessionManager(resume=False)
        context = sm.load_context("测试 system prompt")
        assert len(context) == 1
        assert context[0]["role"] == "system"
        assert context[0]["content"] == "测试 system prompt"

    def test_update_meta(self, sessions_dir):
        """update_meta() 修改后可从文件重新读取"""
        sm = SessionManager(resume=False)
        sm.save_message("user", "test")
        sm.update_meta(summary="测试摘要")

        # 新实例读取 meta
        sm2 = SessionManager(resume=sm.session_id)
        assert sm2._meta.get("summary") == "测试摘要"
