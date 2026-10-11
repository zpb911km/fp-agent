"""platform_utils — 跨平台原语的最小验证。

1. handoff 的 /proc 兜底：macOS / Windows 无 procfs，
   ``perform_exec_reload`` 必须给出可操作的诊断，而非无谓的 ``open()`` 失败。
2. OS 原语（#170）：kill_process_tree / FileLock / restrict_file_permissions /
   restart_self —— 两条平台分支都用 mock 在本机（Linux）验证。
"""

import os
import stat
import subprocess
import sys
import time
import types
from unittest.mock import MagicMock

import pytest

from fp_core import platform_utils
from fp_core.core import handoff


class TestHasProcfs:
    def test_reflects_runtime_probe(self):
        """has_procfs() 应等于运行期探测结果（Linux=True，macOS/Windows=False）。"""
        assert platform_utils.has_procfs() == os.path.isdir("/proc/self")

    def test_true_on_linux(self):
        """Linux 上恒为 True（CI ubuntu-latest / macos-latest 各验其位）。"""
        if platform_utils.is_linux():
            assert platform_utils.has_procfs() is True


class TestExecReloadNoProcfs:
    """无 procfs 平台：缺 FP_LAUNCH_JSON 时不许崩，要给可操作诊断。"""

    @pytest.mark.asyncio
    async def test_returns_actionable_diagnostic(self, monkeypatch):
        monkeypatch.delenv("FP_LAUNCH_JSON", raising=False)
        monkeypatch.setattr(platform_utils, "has_procfs", lambda: False)

        state = MagicMock()
        msg = await handoff.perform_exec_reload(state, kind="command")

        assert isinstance(msg, str) and msg.startswith("❌")
        assert "/proc/self" in msg
        assert "capture_launch_command" in msg


# ═══════════════════════════════════════════════════════════════
# OS 原语 #170：两条平台分支都在本机（Linux）用 mock 验证
# ═══════════════════════════════════════════════════════════════


class TestKillProcessTree:
    def test_posix_kills_process_group(self, monkeypatch):
        seen: list[tuple[int, int]] = []
        fake_signal = 9
        monkeypatch.setattr(platform_utils, "is_windows", lambda: False)
        monkeypatch.setattr(platform_utils.os, "killpg", lambda pid, sig: seen.append((pid, sig)))
        monkeypatch.setattr(platform_utils.signal, "SIGKILL", fake_signal, raising=False)

        platform_utils.kill_process_tree(1234)

        assert seen == [(1234, fake_signal)]

    def test_windows_uses_taskkill_tree(self, monkeypatch):
        seen: dict[str, object] = {}

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(args, **kw):
            seen["args"] = args
            return _R()

        monkeypatch.setattr(platform_utils, "is_windows", lambda: True)
        monkeypatch.setattr(platform_utils.subprocess, "run", fake_run)

        platform_utils.kill_process_tree(4321)

        assert seen["args"] == ["taskkill", "/F", "/T", "/PID", "4321"]

    def test_windows_not_found_maps_to_process_lookup(self, monkeypatch):
        class _R:
            returncode = 128
            stdout = ""
            stderr = 'ERROR: The process "4321" not found.'

        monkeypatch.setattr(platform_utils, "is_windows", lambda: True)
        monkeypatch.setattr(platform_utils.subprocess, "run", lambda args, **kw: _R())

        with pytest.raises(ProcessLookupError):
            platform_utils.kill_process_tree(4321)

    def test_windows_permission_denied_maps_to_permission_error(self, monkeypatch):
        class _R:
            returncode = 5
            stdout = "ERROR: Access is denied."
            stderr = ""

        monkeypatch.setattr(platform_utils, "is_windows", lambda: True)
        monkeypatch.setattr(platform_utils.subprocess, "run", lambda args, **kw: _R())

        with pytest.raises(PermissionError):
            platform_utils.kill_process_tree(4321)


class TestProcessAlive:
    def test_posix_true_for_self(self, monkeypatch):
        monkeypatch.setattr(platform_utils, "is_windows", lambda: False)
        assert platform_utils.process_alive(os.getpid()) is True

    def test_posix_false_for_bogus_pid(self, monkeypatch):
        monkeypatch.setattr(platform_utils, "is_windows", lambda: False)
        assert platform_utils.process_alive(999_999_999) is False

    def test_windows_delegates_to_helper(self, monkeypatch):
        seen: dict[str, int] = {}
        monkeypatch.setattr(platform_utils, "is_windows", lambda: True)
        monkeypatch.setattr(
            platform_utils, "_windows_process_alive", lambda pid: bool(seen.setdefault("pid", pid)) or True
        )

        assert platform_utils.process_alive(42) is True
        assert seen["pid"] == 42


class TestFileLock:
    def test_acquire_release_and_contention(self, tmp_path):
        """第二个 FileLock（独立 fd）拿不到锁 → 单写者互斥成立。"""
        path = str(tmp_path / "a.lock")
        a = platform_utils.FileLock(path)
        b = platform_utils.FileLock(path)

        assert a.acquire() is True
        try:
            assert b.acquire() is False
        finally:
            a.release()

        assert b.acquire() is True
        b.release()

    def test_context_manager_raises_when_busy(self, tmp_path):
        path = str(tmp_path / "b.lock")

        def _enter() -> None:
            with platform_utils.FileLock(path):
                pass

        holder = platform_utils.FileLock(path)
        assert holder.acquire() is True
        try:
            with pytest.raises(BlockingIOError):
                _enter()
        finally:
            holder.release()

    def test_release_before_acquire_is_safe(self, tmp_path):
        platform_utils.FileLock(str(tmp_path / "c.lock")).release()  # 不应抛

    def test_windows_branch_uses_msvcrt(self, tmp_path, monkeypatch):
        """Windows 分支：走 msvcrt.locking，非阻塞码 LK_NBLCK、解锁码 LK_UNLCK。

        平台判定打桩 ``sys.platform``（而非 ``is_windows``）——锁定原语内部用
        ``sys.platform == "win32"`` 直接比较，以便 pyright 做平台收窄。
        """
        calls: list[int] = []
        fake = types.SimpleNamespace(
            LK_LOCK=1,
            LK_NBLCK=2,
            LK_UNLCK=3,
            locking=lambda fd, mode, n: calls.append(mode),
        )
        monkeypatch.setitem(sys.modules, "msvcrt", fake)
        monkeypatch.setattr(sys, "platform", "win32")

        lock = platform_utils.FileLock(str(tmp_path / "d.lock"))
        assert lock.acquire() is True
        lock.release()

        assert calls[0] == 2  # LK_NBLCK
        assert calls[-1] == 3  # LK_UNLCK

    def test_windows_branch_reports_contention(self, tmp_path, monkeypatch):
        """Windows 分支：msvcrt 抛 OSError（已被占用）→ acquire 返回 False，不冒泡。"""

        def _boom(fd, mode, n):
            raise OSError(13, "locked")

        fake = types.SimpleNamespace(LK_LOCK=1, LK_NBLCK=2, LK_UNLCK=3, locking=_boom)
        monkeypatch.setitem(sys.modules, "msvcrt", fake)
        monkeypatch.setattr(sys, "platform", "win32")

        assert platform_utils.FileLock(str(tmp_path / "e.lock")).acquire() is False


class TestRestrictFilePermissions:
    def test_posix_sets_mode(self, tmp_path, monkeypatch):
        monkeypatch.setattr(platform_utils, "is_windows", lambda: False)
        p = tmp_path / "secret"
        p.write_text("x", encoding="utf-8")
        os.chmod(p, 0o644)

        platform_utils.restrict_file_permissions(str(p), 0o600)

        assert stat.S_IMODE(p.stat().st_mode) == 0o600

    def test_windows_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(platform_utils, "is_windows", lambda: True)
        p = tmp_path / "secret2"
        p.write_text("x", encoding="utf-8")
        before = p.stat().st_mode

        platform_utils.restrict_file_permissions(str(p), 0o600)

        assert p.stat().st_mode == before


class TestRestartSelf:
    def test_posix_execve(self, monkeypatch):
        seen: dict[str, object] = {}
        monkeypatch.setattr(platform_utils, "is_windows", lambda: False)
        monkeypatch.setattr(
            platform_utils.os,
            "execve",
            lambda path, argv, env: seen.update(path=path, argv=argv, env=env),
        )

        platform_utils.restart_self(["/usr/bin/fp", "-x"], {"A": "1"})

        assert seen["path"] == "/usr/bin/fp"
        assert seen["argv"] == ["/usr/bin/fp", "-x"]
        assert seen["env"] == {"A": "1"}

    def test_windows_spawns_then_exits(self, monkeypatch):
        """Windows 分支：spawn 新实例后 os._exit(0) 让位，不返回。"""
        seen: dict[str, object] = {}

        class _Proc:
            pass

        def fake_popen(argv, **kw):
            seen["argv"] = argv
            seen["kw"] = kw
            return _Proc()

        monkeypatch.setattr(platform_utils, "is_windows", lambda: True)
        monkeypatch.setattr(platform_utils.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(platform_utils.os, "_exit", lambda code: seen.update(code=code))

        platform_utils.restart_self(["fp.exe", "-x"], {"A": "1"})

        assert seen["argv"] == ["fp.exe", "-x"]
        assert seen["kw"]["env"] == {"A": "1"}
        assert seen["code"] == 0

    def test_restart_self_replaces_process_e2e(self, tmp_path):
        """真·子进程 E2E（无 mock）：restart_self 后新进程接管并写出标记。

        三平台在 CI 各验其位——这是 Windows 上「reload 是否真的换了进程」唯一
        可自动化的证据（无真机可用时的替代）。顺带钉住两平台的语义差异：
          POSIX  → execve 真·映像替换，**PID 不变**（shell 等的是同一个进程）
          Windows→ spawn 新实例 + 旧进程退出，**PID 必变**
        """
        marker = tmp_path / "marker.txt"
        child = tmp_path / "child.py"
        child.write_text(
            f"import os, pathlib\npathlib.Path({str(marker)!r}).write_text(str(os.getpid()), encoding='utf-8')\n",
            encoding="utf-8",
        )
        driver = tmp_path / "driver.py"
        driver.write_text(
            "import os, sys\n"
            "from fp_core.platform_utils import restart_self\n"
            f"restart_self([sys.executable, {str(child)!r}], dict(os.environ))\n"
            "raise SystemExit('restart_self returned unexpectedly')\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)

        proc = subprocess.Popen(
            [sys.executable, str(driver)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        out, _ = proc.communicate(timeout=60)

        assert "restart_self returned unexpectedly" not in (out or "")
        for _ in range(100):  # Windows 上新实例可能略晚于旧进程退出 → 有界轮询
            if marker.exists():
                break
            time.sleep(0.05)
        assert marker.exists(), f"新进程未写出标记；driver 输出={out!r}"

        child_pid = int(marker.read_text(encoding="utf-8"))
        if platform_utils.is_windows():
            assert child_pid != proc.pid, "Windows 应 spawn 新进程（PID 必变）"
        else:
            assert child_pid == proc.pid, "POSIX execve 应保持同一 PID"
