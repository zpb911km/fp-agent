"""fp docs — 查看离线文档

随包分发的离线文档位于 site-packages/fp/docs/（由 release.yml 注入）。
本模块提供查看入口：打印路径、列出目录树、或调用系统默认方式打开。
"""

import os
import subprocess
import sys

DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
ONLINE_URL = "https://github.com/zpb911km/fp-agent/tree/main/docs"


def _docs_available() -> bool:
    return os.path.isdir(DOCS_DIR) and os.path.exists(os.path.join(DOCS_DIR, "README.md"))


def _open_path(path: str) -> bool:
    """用系统默认方式打开路径（文件管理器 / 浏览器）。"""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


def _print_tree() -> None:
    for root, dirs, files in os.walk(DOCS_DIR):
        dirs.sort()
        level = root.replace(DOCS_DIR, "").count(os.sep)
        indent = "  " * level
        print(f"{indent}{os.path.basename(root) or 'docs'}/")
        for f in sorted(files):
            print(f"{indent}  {f}")


def open_docs(*extra: str) -> int:
    """入口：fp docs [--path | --list]。

    --path/-p  仅打印文档目录绝对路径
    --list/-l  列出文档目录树
    （默认）    调用系统默认方式打开文档目录
    """
    show_path = any(a in ("--path", "-p") for a in extra)
    show_list = any(a in ("--list", "-l") for a in extra)

    if not _docs_available():
        print(f"⚠️  未找到离线文档：{DOCS_DIR}")
        print("当前安装版本可能不含离线文档，请升级：pip install -U fp-agent")
        print(f"在线文档：{ONLINE_URL}")
        return 1

    print(f"📚 离线文档目录：{DOCS_DIR}")

    if show_path:
        print(DOCS_DIR)
        return 0

    if show_list:
        _print_tree()
        return 0

    print("正在打开…（如未弹出，请手动打开上述目录）")
    if _open_path(DOCS_DIR):
        return 0
    print("⚠️  未能自动打开，请手动访问上述路径。")
    return 0
