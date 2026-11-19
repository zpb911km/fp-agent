"""cwd 是会话属性 —— 装载即绑定，所有路径一致。

为什么必须有这层测试：`current_cwd()` 本身（`test_cwd.py`）只证明 contextvar
能存能取；**真正会出错的是「谁在什么时候把它设成什么」**。改造前 cwd 只有
`os.chdir`（进程级），resume 旧会话必定落在启动目录 —— 语义是「进程的」而不是
「会话的」，而这个错误**不报异常、不写日志**：文件静默写进另一个仓库。

所以这里断言的是三条装载路径的不变量（删掉 `_bind_session_cwd` 全部必红）：
1. 切换会话 → 相对路径基准 = 该会话的 cwd（不是启动目录、不是上一个会话）；
2. 新建会话 → 继承当前基准（首个会话即启动目录），并**立即落盘**会话根
   （否则切走就丢，切回来只能瞎猜）；
3. 无 cwd 的老会话 / 目录已被删 → 保持当前基准不动（向后兼容 + 可诊断），
   进程重启（reload）后能靠落盘的 meta 恢复。
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

import fp_core.config as cfg
import fp_core.core.session as session_mod
from fp_core.core.cwd import current_cwd, reset_cwd, resolve_path, set_cwd
from fp_core.core.session import SessionManager


def _read_header(path: str) -> dict:
    """读会话文件首行（meta header）。"""
    with open(path, encoding="utf-8") as f:
        return json.loads(f.readline())


def _read_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return f.read().splitlines()


@pytest.fixture
def sessions_dir(tmp_path):
    """临时会话目录（patch 所有引用点）。"""
    d = str(tmp_path / "sessions")
    os.makedirs(d, exist_ok=True)

    with patch.object(cfg, "SESSIONS_DIR", d), patch.object(session_mod, "SESSIONS_DIR", d):
        yield d


@pytest.fixture
def cwd_isolated():
    """每个用例把 contextvar 复位，避免 `set_cwd` 残留污染同进程后续用例。"""
    token = set_cwd(os.getcwd())
    try:
        yield
    finally:
        reset_cwd(token)


@pytest.fixture
def roots(tmp_path, cwd_isolated):
    a = tmp_path / "projA"
    b = tmp_path / "projB"
    a.mkdir()
    b.mkdir()
    return str(a), str(b)


class TestSessionRootBinding:
    """装载会话即切换相对路径基准"""

    def test_switch_session_restores_its_own_root(self, sessions_dir, roots):
        """在 A 建会话、在 B 建会话，来回切换时基准跟随各自的根。

        变异敏感：把 `_bind_session_cwd` 去掉 → 基准恒为最后一次设置的值，必红。
        """
        a, b = roots
        sm = SessionManager(resume=False)
        sid_a = sm.create_session(cwd=a)
        assert current_cwd() == a
        sid_b = sm.create_session(cwd=b)
        assert current_cwd() == b

        assert sm.switch_session(sid_a) is True
        assert current_cwd() == a
        assert resolve_path("x.txt") == os.path.join(a, "x.txt")

        assert sm.switch_session(sid_b) is True
        assert current_cwd() == b
        assert resolve_path("x.txt") == os.path.join(b, "x.txt")

    def test_new_session_inherits_current_root(self, sessions_dir, roots):
        """`/new` 无参 → 继承当前基准（首个会话即启动目录，语义与改造前一致）。"""
        a, _ = roots
        sm = SessionManager(resume=False)
        sm.create_session(cwd=a)
        sid = sm.create_session()  # 不传 cwd
        assert current_cwd() == a
        meta = _read_header(sm.get_session_path())
        assert meta["id"] == sid
        assert meta["cwd"] == a, "继承来的根也必须写进 meta"

    def test_create_persists_root_immediately(self, sessions_dir, roots):
        """会话根必须**创建即落盘**：只放内存的话切走即丢。"""
        a, b = roots
        sm = SessionManager(resume=False)
        sid = sm.create_session(cwd=a)
        path = sm.get_session_path()
        assert os.path.exists(path), "cwd 是会话身份，不能靠惰性落盘"
        meta = _read_header(path)
        assert meta["cwd"] == a

        # 切到别的会话后，A 的根仍在盘上 → 切回来能复位
        sm.create_session(cwd=b)
        assert sm.switch_session(sid) is True
        assert current_cwd() == a

    def test_legacy_session_without_cwd_keeps_current(self, sessions_dir, roots):
        """改造前的老会话没有 `cwd` 字段 → 不猜，保持当前基准（向后兼容）。"""
        a, b = roots
        sm = SessionManager(resume=False)
        sid_a = sm.create_session(cwd=a)
        sid_b = sm.create_session(cwd=b)

        # 抹掉 a 的 cwd，模拟老会话
        path = sm.get_session_path()  # 当前是 b
        sm.switch_session(sid_a)
        a_path = sm.get_session_path()
        lines = _read_lines(a_path)
        meta = json.loads(lines[0])
        meta.pop("cwd", None)
        with open(a_path, "w", encoding="utf-8") as f:
            f.write("\n".join([json.dumps(meta, ensure_ascii=False), *lines[1:]]) + "\n")

        sm.switch_session(sid_b)
        assert current_cwd() == b
        sm.switch_session(sid_a)
        assert current_cwd() == b, "无 cwd 的老会话不该把基准改成猜出来的值"
        assert os.path.exists(path)

    def test_missing_directory_keeps_current(self, sessions_dir, roots, tmp_path):
        """会话根被删/改名 → 保持当前基准（设进去只会让每个工具都撞 ENOENT）。"""
        _, b = roots
        gone = tmp_path / "gone"
        gone.mkdir()
        sm = SessionManager(resume=False)
        sid_gone = sm.create_session(cwd=str(gone))
        sm.create_session(cwd=b)
        gone.rmdir()

        assert sm.switch_session(sid_gone) is True
        assert current_cwd() == b

    def test_resume_latest_restores_root_after_restart(self, sessions_dir, roots):
        """进程重启（reload）后 resume 最新会话 → 基准仍是它自己的根。

        这条覆盖的是「落盘」而非「绑定」：如果 cwd 没写进 meta，新进程只能
        按启动目录猜，用户会发现 resume 之后的工具都落在错误目录。
        """
        a, _ = roots
        sm1 = SessionManager(resume=False)
        sid = sm1.create_session(cwd=a)
        sm1.save_message("user", "在这条会话里")

        set_cwd(os.getcwd())  # 模拟新进程从启动目录开始
        sm2 = SessionManager(resume=True)
        assert sm2.session_id == sid
        assert current_cwd() == a

    def test_launch_snapshot_records_session_root(self, cwd_isolated, roots, monkeypatch):
        """reload 是 exec 重启：启动快照里的 cwd 必须是**会话根**，不是进程目录。

        这是「reload 后还站在同一个项目里」的唯一依据（`perform_exec_reload` 按
        快照 `os.chdir` 后才 exec）。若这里记进程目录，跨项目会话 reload 一次就
        被拽回启动目录 —— 而且下次 resume 又绑回会话根，形成「每 reload 抖一下」。
        """
        from fp_core.core.handoff import capture_launch_command

        a, _ = roots
        monkeypatch.delenv("FP_LAUNCH_JSON", raising=False)
        tok = set_cwd(a)
        try:
            capture_launch_command()
        finally:
            reset_cwd(tok)
        launch = json.loads(os.environ["FP_LAUNCH_JSON"])
        assert launch["cwd"] == a
        monkeypatch.delenv("FP_LAUNCH_JSON", raising=False)

    def test_empty_session_still_hidden_from_list(self, sessions_dir, roots):
        """落盘不等于刷屏：0 长度会话仍不进列表（非当前会话）。"""
        a, b = roots
        sm = SessionManager(resume=False)
        sm.create_session(cwd=a)
        sm.create_session(cwd=b)  # 这条是当前会话，且没有消息
        listed = sm.list_sessions()
        assert len(listed) == 1, "只有当前会话可见，空会话不刷屏"
        assert sm.session_id in listed

    def test_clear_keeps_root_identity(self, sessions_dir, roots, tmp_path):
        """`/clear` 清的是消息，不是会话身份：`cwd` 必须原样留在 meta 里。

        变异敏感：`_default_meta(self._session_id)` 不显式续传旧 cwd 时，
        目录已被删的会话（装载时跳过绑定，当前基准 ≠ 会话根）会在 /clear
        之后把 `cwd` 悄悄改写成进程目录 —— 静默的身份漂移。
        """
        _, b = roots
        gone = tmp_path / "gone"
        gone.mkdir()
        sm = SessionManager(resume=False)
        sid = sm.create_session(cwd=str(gone))
        sm.create_session(cwd=b)  # 切走，当前基准 = b
        gone.rmdir()  # 会话根消失

        assert sm.switch_session(sid) is True
        assert current_cwd() == b, "目录不存在 → 不绑定，保持当前基准"
        sm.clear_session_file()
        meta = _read_header(sm.get_session_path())
        assert meta["cwd"] == str(gone), "清消息不该改写会话根"
        assert current_cwd() == b, "清消息也不该动当前基准"


class TestReloadCarriesSessionRoot:
    """exec 重启要把会话根带过进程边界"""

    @pytest.mark.asyncio
    async def test_reload_launch_snapshot_follows_session(self, sessions_dir, roots, monkeypatch):
        """「在 B 启动 → 续上 A 的会话 → /reload」→ 新进程必须回到 A。

        `capture_launch_command()` 用 setdefault（重启链恒为首次 argv/cwd），
        所以快照里的 cwd 停在 B。若不在 exec 前覆写，新进程的**进程级 cwd**
        就是 B：core 侧还能靠 `_bind_session_cwd` 兜住相对路径，但
        `os.getcwd()` 的消费者（zeta workspace 归属、子进程继承）全错到 B。
        """
        from fp_core.core import handoff as handoff_mod
        from fp_core.core.agent import Agent
        from fp_core.platform_utils import get_data_dir

        os.makedirs(get_data_dir(), exist_ok=True)  # handoff 文件要写这里（测试环境是全新临时根）

        a, _ = roots
        agent = Agent(enable_log=False)
        agent.state.session.create_session(cwd=a)
        assert current_cwd() == a

        # 首次启动快照：argv 是真实入口，cwd 是当时的启动目录（这里用 /tmp 代表 B）
        monkeypatch.setenv("FP_LAUNCH_JSON", json.dumps({"argv": ["/bin/true"], "cwd": "/tmp"}))
        seen: dict[str, str] = {}

        def fake_restart(argv: list[str], env: dict[str, str]) -> None:
            seen["launch"] = env["FP_LAUNCH_JSON"]

        monkeypatch.setattr(handoff_mod, "restart_self", fake_restart)

        out = await handoff_mod.perform_exec_reload(agent.state, kind="command")
        assert out.startswith("❌"), f"restart_self 返回即「意外返回」，实际: {out}"
        assert "launch" in seen, f"restart_self 未被调用，核心提前返回: {out}"
        launch = json.loads(seen["launch"])
        assert launch["cwd"] == a, "reload 必须回到会话根，而不是首次启动目录"
        assert launch["argv"] == ["/bin/true"], "argv 仍用首次启动命令"
