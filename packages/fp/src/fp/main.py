"""fp - 顶层路由入口"""

import argparse
import asyncio
import sys


def main():
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

    args, rest = parser.parse_known_args()

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
