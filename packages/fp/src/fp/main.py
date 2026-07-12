"""fp - 顶层路由入口"""

import argparse
import asyncio
import os
import sys


def main():
    from fp.version_checker import check_updates_background

    check_updates_background()

    parser = argparse.ArgumentParser("fp")
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

    args, rest = parser.parse_known_args()

    if args.version:
        from fp_core import __version__

        print(f"fp {__version__}")
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
            print("请安装: pip install fp[webui]")
            sys.exit(1)
        run_webui()

    elif args.mode == "acp":
        try:
            from fp_acp import run as run_acp
        except ImportError:
            print("请安装: pip install fp[acp]")
            sys.exit(1)
        run_acp()


if __name__ == "__main__":
    main()
