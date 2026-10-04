"""fp - 顶层路由入口"""

import argparse
import asyncio
import os
import sys

_MODES = ("cli", "webui", "acp")


def _normalize_mode_argv(argv: list[str]) -> list[str]:
    """把短选项 -m 的双义用法摊平成无歧义长选项。

    -m 的取值 ∈ {cli,webui,acp} → --mode  （选启动模式，兼容旧用法）
    -m 的其他取值              → --message（透传给 fp_cli 的单次消息）

    摊平后顶层 argparse 永远见不到裸 -m：否则 `fp -m "你好"` 会被
    --mode 的 choices 校验拦下（invalid choice），而文档承诺的单次消息
    用法根本跑不通。`--message` 是 fp_cli 的长选项，顶层不注册，
    parse_known_args 会原样放进 rest 透传下去。
    """
    out: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else ""
        if tok == "-m" and nxt and not nxt.startswith("-"):
            out += ["--mode" if nxt in _MODES else "--message", nxt]
            i += 2
        elif tok.startswith("-m=") and len(tok) > 3:
            # 等号形式：-m=webui / -m=你好
            val = tok[3:]
            out.append(f"--mode={val}" if val in _MODES else f"--message={val}")
            i += 1
        elif tok.startswith("-m") and not tok.startswith("--") and len(tok) > 2:
            # 粘连形式：-mwebui / -m你好
            val = tok[2:]
            out.append(f"--mode={val}" if val in _MODES else f"--message={val}")
            i += 1
        else:
            out.append(tok)
            i += 1
    return out


_EPILOG = """\
子命令（在顶层解析前路由，参数互不干扰）：
  fp docs [路径 | --list | --path | --open]   离线文档（详见 fp docs）
  fp ext <子命令>                             扩展资产分发（详见 fp ext -h）

透传给 CLI 模式（--mode cli，由 fp_cli 解析）：
  -m, --message MSG     单次消息模式：发送一条后退出
  -r, --resume [SID]    恢复会话；省略 SID 时恢复最近一次
  --init                初始化配置文件后退出
  --headless            无头驻留：跳过 logo 与提示符，常驻至 Ctrl+C/SIGTERM
                        （可与 -r 组合；管道/服务管理器拉起不因 stdin EOF 退出）

透传给 WebUI 模式（--mode webui，由 fp_webui 解析）：
  --host HOST           监听地址（默认 127.0.0.1）
  --port PORT           监听端口（默认 8765）
  --expose              监听 0.0.0.0，允许局域网设备访问
  --reload              热重载（开发用）

注：-m 取值为 cli|webui|acp 时视为 --mode，否则视为 --message（单次消息）。

示例：
  fp                              交互模式
  fp -m "你好"                    单次消息后退出
  fp -r                           恢复最近会话
  fp --headless -r s_abc123       无头驻留并恢复指定会话
  fp --mode webui --port 9000     启动 WebUI（--expose 允许局域网访问）
  fp --model provider/model       本次启动覆盖激活模型
  fp docs --list                  列出离线文档
"""


def main():
    # ── --model 预扫描：必须先于 fp_core 的首次 import ──
    # fp_core.config 在模块加载时一次性解析激活 LLM（LLM_* 常量就此定值），
    # 等 argparse 跑完再设 FP_MODEL 就太晚了。只读 argv、不改写，
    # 不违反 bootstrap「argv 改写前捕获启动命令」的契约。
    _raw = sys.argv[1:]
    for _i, _tok in enumerate(_raw):
        if _tok == "--model" and _i + 1 < len(_raw):
            os.environ["FP_MODEL"] = _raw[_i + 1]
            break
        if _tok.startswith("--model="):
            os.environ["FP_MODEL"] = _tok.split("=", 1)[1]
            break

    # ── 捕获原始启动命令（reload 激活核心 execve 重启用，新入口契约第 0 步）──
    # 必须在任何 argv 改写/前置路由之前调用；契约见 fp_core.core.handoff。
    from fp_core.api import portal

    portal.ctl.bootstrap()

    # ── 子命令前置路由：fp ext / fp docs 在顶层 argparse 之前识别 ──
    # 若在 parse_known_args 之后才识别，`fp ext -h` 的 -h 会被顶层解析劫持，
    # 显示顶层帮助而非子命令帮助。前置路由让子命令拿到原始参数。
    argv_rest = sys.argv[1:]
    if argv_rest[:1] == ["ext"]:
        from fp.ext import ext_main

        sys.exit(ext_main(argv_rest[1:]))
    if argv_rest[:1] == ["docs"]:
        from fp.docs import open_docs

        sys.exit(open_docs(*argv_rest[1:]))

    parser = argparse.ArgumentParser(
        "fp",
        description="FP — AI Agent 命令行入口（顶层只管分发，各模式参数由对应前端解析）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EPILOG,
    )
    parser.add_argument(
        "--docs",
        action="store_true",
        help="查看离线文档（等价于 fp docs）",
    )
    parser.add_argument(
        "--mode",
        "-m",
        choices=list(_MODES),
        default="cli",
        help="启动模式（默认 cli）。短写 -m 取值为模式名时等价",
    )
    parser.add_argument(
        "--model",
        help="覆盖本次激活模型：provider/model，或 LLM_PROVIDERS 表内裸模型名；未命中则回退配置激活项并告警",
    )
    parser.add_argument(
        "--session",
        help="恢复指定会话 ID（等价于 CLI 的 -r <SID>；webui/acp 模式同样生效）",
    )
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

    args, rest = parser.parse_known_args(_normalize_mode_argv(sys.argv[1:]))

    # fp --docs（等价形式；fp docs 已在函数开头前置路由）
    if args.docs:
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

    # ── 转发 --session：cli 走 -r（fp_cli 原生 resume，显式 SID 后置覆盖
    #    用户同时给的 -r auto）；webui/acp 走 FP_RELOAD_SID —— 那是
    #    portal.ctl.open(resume=...) 的约定通道（见 fp_core.api.portal），
    #    无 FP_RELOAD_HANDOFF 时仅作纯 resume，不会触发 reload 续接。
    if args.session:
        if args.mode == "cli":
            rest = [*rest, "-r", args.session]
        else:
            os.environ["FP_RELOAD_SID"] = args.session

    # ── 修复：清理 sys.argv，只保留子包能识别的剩余参数 ──
    # 子包（fp_cli/fp_webui/fp_acp）各自有自己的 ArgumentParser，
    # 会在各自的 run()/main() 中再次解析 sys.argv。
    # 如果不清理，顶层已消费的参数（如 --mode webui）会被子包 parser
    # 报 "unrecognized arguments" 导致崩溃。
    sys.argv = [sys.argv[0]] + rest

    # ── 兜底回写 FP_MODEL（预扫描已设；此处防 argparse 形式与预扫描不一致）──
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
