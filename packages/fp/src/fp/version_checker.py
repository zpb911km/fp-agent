"""fp/version_checker.py - 自动检测 PyPI 版本更新

每次启动时在后台线程查询 PyPI API，若有新版本则打印提示。
不缓存、不阻塞、失败静默。
"""

import json
import threading
import urllib.request

YELLOW = "\033[33m"
RESET = "\033[0m"

# 需要检查的包：(PyPI 包名, 显示名称)
PACKAGES = [
    ("fp-agent", "fp"),
    ("fp-core", "fp-core"),
    ("fp-terminal", "fp-terminal"),
    ("fp-webui", "fp-webui"),
    ("fp-acp", "fp-acp"),
]


# ── 工具函数 ───────────────────────────────────────


def _parse_version(ver: str) -> tuple:
    """将 '0.1.5' 转为 (0, 1, 5) 以便逐段比较"""
    try:
        return tuple(int(x) for x in ver.split("."))
    except (ValueError, AttributeError):
        return (0,)


def _get_installed_version(pkg_name: str) -> str | None:
    """用 importlib.metadata 获取已安装版本"""
    try:
        from importlib.metadata import version

        return version(pkg_name)
    except Exception:
        return None


def _fetch_latest_version(pkg_name: str) -> str | None:
    """查询 PyPI JSON API 获取最新版本号"""
    url = f"https://pypi.org/pypi/{pkg_name}/json"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "fp-agent/version-checker/1.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data["info"]["version"]
    except Exception:
        return None


def _print_notice(outdated: list):
    """打印黄色更新提示框（到 stderr，不污染 stdout）"""
    lines = [
        f"{YELLOW}╔══════════════════════════════════════════╗",
        "║  ✨ 新版本可用！使用 pip install -U 更新 ║",
        "╚══════════════════════════════════════════╝",
    ]
    for name, old, new in outdated:
        lines.append(f"   {name:<12}  {old} → {new}")
    lines += [
        f"{RESET}",
    ]
    print("\n" + "\n".join(lines) + "\n", file=__import__("sys").stderr)


# ── 核心逻辑 ───────────────────────────────────────


def _do_check():
    """执行一次版本检查"""
    outdated = []

    for pypi_name, display_name in PACKAGES:
        installed = _get_installed_version(pypi_name)
        if installed is None:
            continue

        latest = _fetch_latest_version(pypi_name)
        if latest is None:
            continue

        if _parse_version(latest) > _parse_version(installed):
            outdated.append((display_name, installed, latest))

    if outdated:
        _print_notice(outdated)


def check_updates_background():
    """在后台守护线程中执行版本检查，不阻塞启动。"""
    thread = threading.Thread(target=_do_check, daemon=True)
    thread.start()
