"""
platform_utils.py - 跨平台工具集

单一真相源（Single Source of Truth），所有模块都通过此模块获取：
  - 平台类型（linux / windows / darwin）
  - 数据目录（XDG_DATA_HOME ~/.local/share <-> %APPDATA%）
  - 配置目录（XDG_CONFIG_HOME ~/.config <-> %APPDATA%）
  - Git Bash 定位（Windows 专用）
"""

import os
import shutil
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


def find_bash() -> str | None:
    """在 Windows 上找到 Git Bash 的 bash.exe，找不到返回 None

    搜索路径优先级：
      1. PATH 中的 bash（Git Bash 安装时默认添加）
      2. 常见安装路径
      3. Git for Windows SDK 路径

    Linux/macOS 上始终返回 "bash"（系统自带）。
    """
    if not is_windows():
        return "bash"  # Unix 系统直接用系统 bash

    # 1. PATH 中查找
    resolved = shutil.which("bash")
    if resolved:
        return resolved

    # 2. 常见安装路径
    common_paths = [
        os.path.join("C:", os.sep, "Program Files", "Git", "bin", "bash.exe"),
        os.path.join("C:", os.sep, "Program Files (x86)", "Git", "bin", "bash.exe"),
        os.path.join(os.path.expanduser("~"), "AppData", "Local", "Programs", "Git", "bin", "bash.exe"),
        # MSYS2
        os.path.join("C:", os.sep, "msys64", "usr", "bin", "bash.exe"),
        os.path.join("C:", os.sep, "msys32", "usr", "bin", "bash.exe"),
    ]
    for path in common_paths:
        if os.path.isfile(path):
            return path

    return None


def check_git_bash() -> tuple[bool, str]:
    """检查 Git Bash 是否可用（Windows 专用），返回 (可用?, 消息)

    Linux/macOS 上始终返回 (True, "系统自带 bash")。
    """
    if not is_windows():
        return True, "系统自带 bash"

    bash_path = find_bash()
    if bash_path:
        return True, f"已找到 Git Bash: {bash_path}"
    return False, "未找到 Git Bash。请安装 Git for Windows (https://git-scm.com) 并确保将 Git Bash 添加到 PATH"


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
        # 兜底：使用 colorama 的 stream 检测
        try:
            import colorama.ansitowin32  # type: ignore[import-untyped]

            return colorama.ansitowin32.is_ansi_enabled()
        except ImportError:
            return False

    return True
