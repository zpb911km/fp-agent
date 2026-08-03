"""fp - 顶层路由入口"""

import argparse
import asyncio
import os
import sys


def main():
    parser = argparse.ArgumentParser(
        "fp",
        epilog="子命令：fp docs — 查看离线文档（等价于 fp --docs）",
    )
    parser.add_argument(
        "--docs",
        action="store_true",
        help="查看离线文档（等价于 fp docs）",
    )
    parser.add_argument(
        "--mode",
        "-m",
        choices=["cli", "webui", "acp"],
        default="cli",
        help="启动模式（默认 cli）",
    )
    parser.add_argument("--model", help="指定模型")
    parser.add_argument("--session", help="指定会话 ID")
    parser.add_argument(
        "--version",
        action="store_true",
        help="显示版本号并退出",
    )
    parser.add_argument(
        "--update",
        "-u",
        action="store_true",
        help="检查并自动更新所有 fp 组件",
    )

    args, rest = parser.parse_known_args()

    # ── 子命令：fp docs / fp --docs — 查看离线文档（不触发更新检查） ──
    # 注意：不能用 argparse 位置参数实现 fp docs——
    # 位置参数（nargs="?"）会吞掉透传给子包的首个非选项参数
    # （如 fp -r s_xxx 中的 s_xxx）并触发 choices 校验，破坏参数透传。
    # 这里手动识别 rest 首项是否为 "docs"。
    if rest[:1] == ["docs"]:
        rest = rest[1:]
        is_docs = True
    else:
        is_docs = False

    # ── 子命令：fp ext — 扩展资产分发（纯 CLI 管道，不触发更新检查） ──
    if rest[:1] == ["ext"]:
        rest = rest[1:]
        from fp.ext import ext_main

        sys.exit(ext_main(rest))

    if args.docs or is_docs:
        from fp.docs import open_docs

        sys.exit(open_docs(*rest))

    # ── 存量迁移（幂等）：core 只认三目录，老结构自动并入 private/ ──
    from fp.ext_migrate import ensure as ext_migrate_ensure

    ext_migrate_ensure()

    from fp.version_checker import check_updates_background

    check_updates_background()

    if args.version:
        from fp_core import __version__

        print(f"fp {__version__}")
        sys.exit(0)

    if args.update:
        from fp.version_checker import do_update

        do_update()
        sys.exit(0)

    # ── 修复：清理 sys.argv，只保留子包能识别的剩余参数 ──
    # 子包（fp_cli/fp_webui/fp_acp）各自有自己的 ArgumentParser，
    # 会在各自的 run()/main() 中再次解析 sys.argv。
    # 如果不清理，顶层已消费的参数（如 -m webui）会被子包 parser
    # 报 "unrecognized arguments" 导致崩溃。
    sys.argv = [sys.argv[0]] + rest

    # ── 转发 --model 到子包（通过环境变量，不侵入子包接口） ──
    if args.model:
        os.environ["FP_MODEL"] = args.model

    if args.mode == "cli":
        from fp_cli import run

        asyncio.run(run())

    elif args.mode == "webui":
        try:
            from fp_webui import run as run_webui
        except ImportError:
            print("请安装: pip install fp-agent[webui]")
            sys.exit(1)
        run_webui()

    elif args.mode == "acp":
        try:
            from fp_acp import run as run_acp
        except ImportError:
            print("请安装: pip install fp-agent[acp]")
            sys.exit(1)
        run_acp()


if __name__ == "__main__":
    main()
