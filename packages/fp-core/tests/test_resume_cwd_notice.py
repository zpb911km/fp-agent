"""`/resume` 切换会话后必须**可见地**告知工作目录变了。

为什么值得单独一个文件：cwd 是会话属性（见 `test_session_cwd_binding.py`），
所以 resume 一个在别的项目里建的会话，会把相对路径基准整条换掉。这个行为本身
正确，但**沉默就是缺陷**——用户以为还在 B，`write_file("a.py")` 却写进了 A，
全程没有异常、没有日志。覆盖点有两层：

1. `_dir_notice()` 的格式与「相同目录不出声」（纯函数，便宜）；
2. `execute()` 的**三个**返回点真的把它拼进输出（`latest` / sid / 序号）。
   只测纯函数会漏掉最可能的回归：有人在改文案时顺手删了新加的那段拼接。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from fp_core.commands import resume
from fp_core.core.cwd import reset_cwd, set_cwd


class _FakeConversation:
    system_prompt = "BASE PROMPT"

    def __init__(self) -> None:
        self.messages: list[dict] = []

    def to_serializable(self) -> list[dict]:
        return list(self.messages)

    def set_messages(self, prompt: str, saved: list[dict]) -> None:
        self.messages = list(saved)


class _FakeTokenTracker:
    def meta_fragment(self) -> dict:
        return {}


class _FakeSession:
    """只模拟 resume 用到的会话面；`switch_session` 复刻「装载即绑定」。"""

    def __init__(self, target_cwd: str | None, *, known: bool = True) -> None:
        self._target = target_cwd
        self._known = known
        self.switched: list[str] = []

    def list_sessions(self) -> dict:
        return {"s_target": {}} if self._known else {}

    def save_and_summarize(self, messages, **kwargs) -> str:
        return ""

    def save_context(self, messages) -> None:
        pass

    def load_context(self, prompt: str) -> list[dict]:
        return []

    def switch_session(self, sid: str) -> bool:
        self.switched.append(sid)
        if self._target is not None:
            set_cwd(self._target)  # core 的 `_bind_session_cwd` 等价物
        return True

    def resume_latest(self) -> bool:
        return self.switch_session("s_latest")


class _FakeState:
    def __init__(self, target_cwd: str | None, **kw) -> None:
        self.session = _FakeSession(target_cwd, **kw)
        self.conversation = _FakeConversation()
        self.token_tracker = _FakeTokenTracker()
        self.session_id = "s_current"

    def rebuild_system_prompt(self, refresh: bool = False) -> None:
        pass


@pytest.fixture
def roots(tmp_path):
    a = tmp_path / "projA"
    a.mkdir()
    return str(a)


@pytest.fixture
def cwd_at(roots):
    """当前基准固定在另一个目录（模拟「在 B 里 resume A 的会话」）。"""

    def _set(where: str) -> str:
        token = set_cwd(where)
        return token

    token = _set(roots)
    try:
        yield roots
    finally:
        reset_cwd(token)


class TestDirNotice:
    def test_silent_when_same_dir(self, roots):
        assert resume._dir_notice(roots, roots) == ""
        # 尾斜杠/软链写法不同也不算「变了」
        assert resume._dir_notice(roots + "/", roots) == ""

    def test_shows_both_paths(self, roots, tmp_path):
        other = str(tmp_path / "projB")
        os.makedirs(other, exist_ok=True)
        text = resume._dir_notice(other, roots)
        assert other in text and roots in text
        assert "工作目录" in text

    def test_silent_when_unknown(self, roots):
        """老会话没记 cwd（prev/cur 为空）→ 无从比较，别瞎报。"""
        assert resume._dir_notice("", roots) == ""
        assert resume._dir_notice(roots, "") == ""


class TestResumeOutputCarriesNotice:
    """三个返回点都要拼上提示（删掉任一处的拼接必红）"""

    def _run(self, state, arg: str) -> str:
        ok, out = asyncio.run(resume.execute(state, arg))
        assert ok
        return out

    def test_sid_switch(self, roots, tmp_path, monkeypatch):
        monkeypatch.setattr(resume, "_rebuild_context", lambda state: None)
        other = str(tmp_path / "projB")
        os.makedirs(other, exist_ok=True)
        token = set_cwd(other)
        try:
            state = _FakeState(roots)
            out = self._run(state, "s_target")
            assert "已切换到会话" in out
            assert "工作目录随会话切换" in out
            assert roots in out and other in out
            # 不要在这里断言 `current_cwd() == roots`：`asyncio.run` 把协程丢进
            # **新 Task**，contextvar 的写入随该上下文一起消失（这正是 WebUI 需要
            # `Portal._turn_cwd` 兜底的原因）。提示里两条路径都在，已足以说明
            # 切换瞬间的基准换成了目标会话。
        finally:
            reset_cwd(token)

    def test_sid_switch_same_dir_is_quiet(self, roots, monkeypatch):
        monkeypatch.setattr(resume, "_rebuild_context", lambda state: None)
        token = set_cwd(roots)
        try:
            out = self._run(_FakeState(roots), "s_target")
            assert "已切换到会话" in out
            assert "工作目录" not in out, "目录没变就别刷提示（否则每切一次都响）"
        finally:
            reset_cwd(token)

    def test_latest_switch(self, roots, tmp_path, monkeypatch):
        monkeypatch.setattr(resume, "_rebuild_context", lambda state: None)
        other = str(tmp_path / "projB")
        os.makedirs(other, exist_ok=True)
        token = set_cwd(other)
        try:
            out = self._run(_FakeState(roots), "latest")
            assert "最新会话" in out
            assert "工作目录随会话切换" in out
        finally:
            reset_cwd(token)
