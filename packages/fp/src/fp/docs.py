"""fp docs — 查看离线文档（面向 agent 设计）

随包分发的离线文档位于 site-packages/fp/docs/（由 release.yml 注入）。
本命令的消费者是 FP agent（自举时读文档），因此一切输出走 stdout、
纯文本、不阻塞——绝不自动调用 xdg-open 等 GUI 程序。
人类若想用系统程序打开，显式加 --open。

用法：
    fp docs                 帮助（用法 + 文档目录）
    fp docs help / --help   同上
    fp docs --list / -l     列出文档目录树
    fp docs --path / -p     仅打印文档目录绝对路径
    fp docs <相对路径>       直接打印文档内容（如 fp docs self/README.md）
    fp docs --open          用系统默认程序打开文档目录（人类交互入口）
"""

import os
import subprocess
import sys

DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")


def _docs_available() -> bool:
    return os.path.isdir(DOCS_DIR) and os.path.exists(os.path.join(DOCS_DIR, "README.md"))


def _open_path(path: str) -> bool:
    """用系统默认方式打开路径（文件管理器 / 浏览器）。仅 --open 时调用。"""
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


def _print_help() -> None:
    print("fp docs — 查看离线文档（随包分发，纯文本输出，不弹窗）")
    print()
    print("用法：")
    print("  fp docs                本帮助")
    print("  fp docs --list / -l    列出文档目录树")
    print("  fp docs --path / -p    仅打印文档目录绝对路径")
    print("  fp docs <相对路径>      打印文档内容，如：fp docs self/README.md")
    print("  fp docs --open         用系统默认程序打开（人类交互用）")
    print()
    print(f"文档目录：{DOCS_DIR}")


def _find_similar(target: str) -> list[str]:
    """按文件名模糊匹配文档（找不到的文件时给出候选）。"""
    key = os.path.basename(target).lower().removesuffix(".md")
    hits: list[str] = []
    for root, dirs, files in os.walk(DOCS_DIR):
        dirs.sort()
        for f in sorted(files):
            if not f.endswith(".md"):
                continue
            if key and key in f.lower():
                hits.append(os.path.relpath(os.path.join(root, f), DOCS_DIR))
    return hits


def _show_file(rel_path: str) -> int:
    """打印指定文档内容。路径安全：解析后必须仍在 DOCS_DIR 内。"""
    target = os.path.realpath(os.path.join(DOCS_DIR, rel_path))
    docs_real = os.path.realpath(DOCS_DIR)
    if not target.startswith(docs_real + os.sep):
        print(f"❌ 非法路径（越出文档目录）：{rel_path}")
        return 1
    if os.path.isdir(target):
        print(f"📁 {rel_path} 是目录，用 fp docs --list 查看结构，或指定具体 .md 文件")
        return 1
    if not os.path.isfile(target):
        print(f"❌ 未找到文档：{rel_path}")
        similar = _find_similar(rel_path)
        if similar:
            print("相近文档：")
            for s in similar[:10]:
                print(f"  fp docs {s}")
        else:
            print("查看全部：fp docs --list")
        return 1
    with open(target, encoding="utf-8") as f:
        sys.stdout.write(f.read())
    return 0


def open_docs(*extra: str) -> int:
    """入口：fp docs [help | --list | --path | --open | <文档相对路径>]。

    面向 agent：默认纯文本输出，绝不自动打开 GUI。
    """
    args = list(extra)

    if args and args[0] in ("-h", "--help", "help"):
        _print_help()
        return 0

    if args and args[0] in ("--path", "-p"):
        print(DOCS_DIR)
        return 0

    if not _docs_available():
        print(f"⚠️  未找到离线文档：{DOCS_DIR}")
        print("当前安装版本可能不含离线文档，请升级：pip install -U fp-agent")
        return 1

    if args and args[0] in ("--list", "-l"):
        _print_tree()
        return 0

    if args and args[0] == "--open":
        print(f"📚 离线文档目录：{DOCS_DIR}")
        if _open_path(DOCS_DIR):
            return 0
        print("⚠️  未能自动打开，请手动访问上述路径。")
        return 1

    if args and not args[0].startswith("-"):
        return _show_file(args[0])

    if args:
        print(f"❌ 未知参数：{args[0]}")
        print()

    # 无参数 / 未知参数 → 帮助（不再自动打开 GUI）
    _print_help()
    return 0
