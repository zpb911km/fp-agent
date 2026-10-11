"""
platform_utils.py - 跨平台工具集

单一真相源（Single Source of Truth），所有模块都通过此模块获取：
  - 平台类型（linux / windows / darwin）
  - 数据目录（XDG_DATA_HOME ~/.local/share <-> %APPDATA%）
  - 配置目录（XDG_CONFIG_HOME ~/.config <-> %APPDATA%）
  - Git Bash 定位（Windows 专用）
  - ANSI 支持检测 / procfs 探测
  - OS 原语：进程树击杀 / 排他文件锁 / 私密权限 / 进程重启

架构铁律：平台差异只准住在本文件。
业务代码一律调用这里的中性原语（kill_process_tree / FileLock /
restart_self / restrict_file_permissions），**不得**在别处写
``if is_windows():``。否则两个月后代码里会长出二十个平台分支。
"""

import contextlib
import os
import signal
import subprocess
import sys
from typing import Any, cast

# ═══════════════════════════════════════════════════════════════
# 平台检测
# ═══════════════════════════════════════════════════════════════


def platform() -> str:
    """返回标准平台名：linux / windows / darwin"""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def is_windows() -> bool:
    """是否为 Windows 系统"""
    return sys.platform == "win32"


def is_linux() -> bool:
    """是否为 Linux 系统"""
    return sys.platform == "linux"


# ═══════════════════════════════════════════════════════════════
# 路径工具（跨平台 XDG 等价物）
# ═══════════════════════════════════════════════════════════════

# 参考规范：
#   Linux:   XDG_CONFIG_HOME (默认 ~/.config)
#            XDG_DATA_HOME   (默认 ~/.local/share)
#   Windows: APPDATA         (C:/Users/<user>/AppData/Roaming)
#            LOCALAPPDATA    (C:/Users/<user>/AppData/Local)
#   macOS:   ~/Library/Application Support


def get_config_dir() -> str:
    """获取跨平台用户配置目录

    Linux:   $XDG_CONFIG_HOME/fp  ->  ~/.config/fp
    Windows: %APPDATA%/fp         ->  C:/Users/<user>/AppData/Roaming/fp
    macOS:   ~/Library/Application Support/fp
    """
    if is_windows():
        base = os.environ.get("APPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Roaming"))
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config"))
    return os.path.join(base, "fp")


def get_data_dir() -> str:
    """获取跨平台用户数据目录

    Linux:   $XDG_DATA_HOME/fp   ->  ~/.local/share/fp
    Windows: %LOCALAPPDATA%/fp   ->  C:/Users/<user>/AppData/Local/fp
    macOS:   ~/Library/Application Support/fp
    """
    if is_windows():
        base = os.environ.get("LOCALAPPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Local"))
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME", os.path.join(os.path.expanduser("~"), ".local", "share"))
    return os.path.join(base, "fp")


# ═══════════════════════════════════════════════════════════════
# Git Bash 定位（Windows）
# ═══════════════════════════════════════════════════════════════


class _UnsetType:
    """哨兵类型，表示「尚未查找过」状态"""

    pass


_UNSET = _UnsetType()
"""哨兵值，区分「未查找过」和「查找结果为 None（没找到）」"""

_bash_cache: str | None | _UnsetType = _UNSET
"""模块级缓存：避免每次调用都走一遍检测流程"""


def _is_wsl_bash_stub(path: str) -> bool:
    """检测是否为 WSL 空壳启动器（Microsoft Bash Launcher）

    WSL 的空壳 bash.exe（C:\\Windows\\System32\\bash.exe）只有 ~84KB，
    真正的 Git Bash 约 1MB。利用这个差异做快速过滤，减少 PowerShell 调用。
    """
    # 快速路径：文件大小 >= 200KB 不可能是 84KB 的 WSL 空壳
    try:
        if os.path.getsize(path) >= 200 * 1024:
            return False
    except OSError:
        return True  # 无法读取 → 保守跳过

    # 精确验证：检查文件描述
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"(Get-Item '{path}').VersionInfo.FileDescription"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return "Microsoft Bash Launcher" in result.stdout
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False  # 无法确认则放行（允许执行）


def find_bash() -> str | None:
    """在 Windows 上找到真正的 Git Bash，找不到返回 None

    搜索路径优先级（逐级降级）：
      0. 模块级缓存                 → 避免重复扫描
      1. 用户配置 BASH_PATH         → config.json / 环境变量
      2. PATH 中的 bash（跳过 WSL 空壳）
      3. 返回 None                  → 降级 cmd.exe

    Linux/macOS 上始终返回 "bash"（系统自带）。
    """
    global _bash_cache

    if not is_windows():
        return "bash"  # Unix 系统直接用系统 bash

    # ── 0. 缓存命中 ──
    if not isinstance(_bash_cache, _UnsetType):
        # isinstance 收窄后：_bash_cache 是 str | None
        return _bash_cache

    # ── 1. 用户配置（最高优先级，不缓存校验失败的结果） ──
    try:
        from fp_core.config import BASH_PATH  # lazy import 避免循环导入

        if BASH_PATH and os.path.isfile(BASH_PATH) and not _is_wsl_bash_stub(BASH_PATH):
            _bash_cache = BASH_PATH
            return _bash_cache
    except ImportError:
        pass  # config 不可用时正常 fallthrough

    # ── 2. PATH 中逐个查找，跳过 WSL 空壳 ──
    for p in os.environ.get("PATH", "").split(os.pathsep):
        p = p.strip('"')
        if not p:
            continue
        full = os.path.join(p, "bash.exe")
        if os.path.isfile(full) and not _is_wsl_bash_stub(full):
            _bash_cache = full
            return _bash_cache

    # ── 3. 全部失败 ──
    _bash_cache = None
    return None


def clear_bash_cache() -> None:
    """清除 bash 缓存，强制下次调用重新检测"""
    global _bash_cache
    _bash_cache = _UNSET


def check_git_bash() -> tuple[bool, str]:
    """检查 Git Bash 是否可用（Windows 专用），返回 (可用?, 消息)

    Linux/macOS 上始终返回 (True, "系统自带 bash")。
    """
    if not is_windows():
        return True, "系统自带 bash"

    bash_path = find_bash()
    if bash_path:
        return True, f"已找到 Git Bash: {bash_path}"
    return False, (
        "未找到 Git Bash。请安装 Git for Windows (https://git-scm.com)，"
        "或在 config.json 中设置 BASH_PATH 为 bash.exe 的完整路径。"
    )


# ═══════════════════════════════════════════════════════════════
# ANSI 颜色支持检测
# ═══════════════════════════════════════════════════════════════


def ansi_supported() -> bool:
    """检测终端是否支持 ANSI 转义码

    Linux/macOS:  始终 True（终端原生支持）
    Windows:      Windows 10+ 的 Windows Terminal / VS Code 集成终端 支持；
                  cmd.exe 仅在 10+ 版本 + ENABLE_VIRTUAL_TERMINAL_PROCESSING 启用时支持
    """
    # 强制开关优先
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("NO_COLOR"):
        return False

    # 非交互式终端不支持
    if not sys.stdout.isatty():
        return False

    if is_windows():
        # Windows 10 build 14393+ 通过虚拟终端处理支持 ANSI
        # 但我们保守检测：如果在 Windows Terminal 或 VS Code 终端中，返回 True
        term = os.environ.get("TERM_PROGRAM", "").lower()
        if term in ("vscode", "microsoft-terminal", "windows-terminal"):
            return True
        # 检查 ConEmu / Cmder
        if os.environ.get("CONEMUANSI") == "ON":
            return True
        # 兜底：通过 Windows API 查询控制台是否已启用虚拟终端处理
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = cast(Any, ctypes.windll.kernel32)  # type: ignore[attr-defined]
            # STD_OUTPUT_HANDLE = -11
            h = kernel32.GetStdHandle(-11)
            mode = wintypes.DWORD()
            if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                return bool(mode.value & 0x0004)
        except Exception:
            pass
        # 实在无法检测时，保守假设不支持
        return False

    return True


# ═══════════════════════════════════════════════════════════════
# procfs 探测
# ═══════════════════════════════════════════════════════════════


def has_procfs() -> bool:
    """本平台是否提供 procfs（``/proc``）。

    Linux:           是（handoff 可经 ``/proc/self`` 重建启动命令）。
    macOS / Windows: 否（无 /proc，需走各自的替代路径）。

    用运行期探测（``/proc/self`` 是否为目录）而非平台名判断——
    Linux 容器 / WSL / 受限环境各有差异，探测比猜名字更可靠。
    """
    return os.path.isdir("/proc/self")


# ═══════════════════════════════════════════════════════════════
# OS 原语：进程击杀
# ═══════════════════════════════════════════════════════════════


def kill_process_tree(pid: int, *, sig: int | None = None) -> None:
    """击杀整个进程树（POSIX 进程组 / Windows 进程树）——单一真相源。

    POSIX:
        ``os.killpg(pid, sig)``——pid 必须是进程组组长。bash 工具以
        ``start_new_session=True`` 起子进程，子进程即组长，故按组击杀
        不会殃及宿主（FP 自身）进程。

    Windows:
        无 POSIX 进程组概念，``start_new_session`` 在 Windows 上是静默
        no-op，``os.killpg`` 根本不存在（AttributeError）。改为交给系统
        自带的 ``taskkill /F /T``——按父子关系递归终止整棵树。

    失败抛 OSError 子类；由调用方决定降级（如退化为 ``proc.kill()``）。
    “进程已消失”归一为 ProcessLookupError，“权限不足”归一为 PermissionError，
    使 POSIX / Windows 两条路径的异常面一致。
    """
    if is_windows():
        try:
            proc = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as e:  # taskkill 自身缺失（极小概率）→ 交调用方降级
            raise PermissionError(f"taskkill 不可用: {e}") from e
        if proc.returncode == 0:
            return
        detail = (proc.stderr or proc.stdout or "").strip()
        lowered = detail.lower()
        # ERROR: The process "1234" not found. / ERROR: 找不到进程
        if "not found" in lowered or "找不到" in detail or proc.returncode == 128:
            raise ProcessLookupError(detail or f"PID {pid} 不存在")
        raise PermissionError(detail or f"taskkill 退出码 {proc.returncode}")
    os.killpg(pid, sig if sig is not None else signal.SIGKILL)


def _windows_process_alive(pid: int) -> bool:
    """Windows 存活探测：OpenProcess + GetExitCodeProcess（STILL_ACTIVE）。

    本文件是平台差异的唯一归属地，故这里直接比较 ``sys.platform`` 而非调用
    ``is_windows()``——pyright 会按 ``sys.platform`` 字面量做平台收窄
    （非 win32 时整段不参与类型检查），函数调用则收窄不了。
    """
    if sys.platform != "win32":
        raise RuntimeError("_windows_process_alive 仅 Windows 可用")

    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # 拒绝访问 = 进程存在但不归当前用户；其余（无效 PID）= 不存在
        return ctypes.get_last_error() == error_access_denied
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def process_alive(pid: int) -> bool:
    """进程是否存活——取代直接调用 ``os.kill(pid, 0)``。

    POSIX:
        ``os.kill(pid, 0)``——0 号信号只做「存在性 + 权限」检查，不投递。
        ProcessLookupError → 不存在；PermissionError → 存在但非本用户。

    Windows:
        ``os.kill`` 在 Windows 上**不支持** 0 号信号做探测（它只认
        ``CTRL_C_EVENT`` / ``CTRL_BREAK_EVENT``，会把其余 sig 当作终止，
        等于拿探测当击杀）。因此改用 OpenProcess + GetExitCodeProcess。
    """
    if is_windows():
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ═══════════════════════════════════════════════════════════════
# OS 原语：排他文件锁（单写者）
# ═══════════════════════════════════════════════════════════════


def _lock_fd(fd: int, blocking: bool) -> bool:
    """对已打开的 fd 加排他锁。拿到 True；非阻塞且被占用 False。"""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            msvcrt.locking(fd, mode, 1)
            return True
        except OSError:
            return False
    import fcntl

    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(fd, flags)
        return True
    except OSError:
        return False


def _unlock_fd(fd: int) -> None:
    """释放 _lock_fd 加的锁（失败静默——fd 即将关闭，锁随 close 释放）。"""
    if sys.platform == "win32":
        import msvcrt

        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)


class FileLock:
    """跨平台排他文件锁——取代散落的 ``import fcntl`` / ``flock``。

    POSIX:   ``fcntl.flock(LOCK_EX)``
    Windows: ``msvcrt.locking(LK_NBLCK)``——锁文件首字节，单写者语义等价。

    用法（非阻塞、拿不到即让位——memory 巩固检测的“另一实例持锁”）::

        lock = FileLock(path)
        if not lock.acquire():
            return "另一实例正在跑，本次跳过"
        try:
            ...
        finally:
            lock.release()

    也可作上下文管理器（``with FileLock(path) as lock:``），
    非阻塞拿不到时抛 ``BlockingIOError``。
    """

    def __init__(self, path: str, *, blocking: bool = False) -> None:
        self._path = path
        self._blocking = blocking
        self._fd: int | None = None

    def acquire(self) -> bool:
        """尝试加锁。True=已持有；False=别人持有（仅非阻塞模式）。"""
        if self._fd is not None:
            return True
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o644)
        if _lock_fd(fd, self._blocking):
            self._fd = fd
            return True
        os.close(fd)
        return False

    def release(self) -> None:
        if self._fd is None:
            return
        _unlock_fd(self._fd)
        os.close(self._fd)
        self._fd = None

    def __enter__(self) -> "FileLock":
        if not self.acquire():
            raise BlockingIOError(f"文件锁被占用: {self._path}")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# ═══════════════════════════════════════════════════════════════
# OS 原语：权限位（POSIX）
# ═══════════════════════════════════════════════════════════════


def restrict_file_permissions(path: str, mode: int = 0o600) -> None:
    """把文件收窄为“仅属主可读写”（默认 0o600）。

    POSIX:   ``os.chmod``——配置里含 API key，必须挡同机其他用户。
    Windows: 无 POSIX 权限位语义（``chmod`` 只能切只读位），此时是
             有意的 no-op——文件系统 ACL 由用户目录默认继承决定，
             强行 chmod 反而会误置只读位、破坏后续写入。
    """
    if is_windows():
        return
    with contextlib.suppress(OSError):
        os.chmod(path, mode)


# ═══════════════════════════════════════════════════════════════
# OS 原语：进程重启（reload / 自举的底层）
# ═══════════════════════════════════════════════════════════════


def restart_self(argv: list[str], env: dict[str, str]) -> None:
    """用 ``argv`` / ``env`` 重启自身——**成功则不返回**。

    取代业务代码里的裸 ``os.execve``，把平台差异收敛到这里。

    POSIX:
        ``os.execve(argv[0], argv, env)``——直接替换进程映像，
        PID 不变，shell 等待的仍是同一个进程。

    Windows:
        ``os.execve`` 在 Windows 上并非真正的映像替换（内部 spawn 新进程
        再终止旧进程，PID 必变，且控制台句柄继承语义与 POSIX 不同），
        行为不像 POSIX 那样可依赖。故显式走「spawn 新实例 + 本进程退出」：
        新进程继承 stdin/stdout/stderr（Windows 下标准句柄默认恒继承，
        即使 close_fds=True），从而继续占用同一控制台；随后
        ``os._exit(0)`` 让位（跳过 atexit/finally——调用方已完成落盘）。

    失败抛 OSError，由调用方回滚（handoff 的救火原则：旧进程不死则无损）。
    """
    if is_windows():
        # 独立进程组：避免控制台的 Ctrl+C 同时打到新旧两个进程
        creationflags = 0
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creationflags |= new_group
        subprocess.Popen(argv, env=env, creationflags=creationflags)
        # _exit 跳过缓冲区刷新，先把已有输出推给控制台，再让位
        with contextlib.suppress(Exception):
            sys.stdout.flush()
            sys.stderr.flush()
        os._exit(0)  # 新实例已接管，旧进程即刻让位（不再执行 finally）
        return  # pragma: no cover — 不可达，仅为静态分析完整性

    # POSIX：真·映像替换。成功即不返回。
    os.execve(argv[0], argv, env)
