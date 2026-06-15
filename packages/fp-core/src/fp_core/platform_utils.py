"""
platform_utils.py - 跨平台工具集

单一真相源（Single Source of Truth），所有模块都通过此模块获取：
  - 平台类型（linux / windows / darwin）
  - 数据目录（XDG_DATA_HOME ~/.local/share <-> %APPDATA%）
  - 配置目录（XDG_CONFIG_HOME ~/.config <-> %APPDATA%）
  - Git Bash 定位（Windows 专用）
"""

import os
import subprocess
import sys

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

            kernel32 = ctypes.windll.kernel32
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
