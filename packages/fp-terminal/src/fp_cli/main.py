"""
命令行交互界面
"""

import asyncio
import contextlib
import os
import signal
import sys
from types import FrameType
from typing import Any

from fp_core import config
from fp_core.api import IOChannel, portal
from fp_core.logger import Logger, set_logger

from . import display
from .appearance import install_appearance, tui_glyph
from .cli_io import CLIIO
from .tui import TUIIO, run_tui, tui_available


# ── Terminal Logger：fp-core 内部日志 → 终端输出 ──
class _TerminalLogger(Logger):
    def info(self, msg: str):
        display.info(msg)

    def warning(self, msg: str):
        display.warning(msg)

    def error(self, msg: str):
        display.error(msg)


set_logger(_TerminalLogger())


async def _read_line(prompt: str) -> str:
    """L1 裸 input（executor 中跑，避免阻塞事件循环）。

    prompt_toolkit 已退场（n10）：L1 是降级路径，无补全/历史——
    补全与历史由 L0 TUI 承担（completion.py / history.py）。
    EOF（Ctrl+D / 管道结束）与 KeyboardInterrupt 原样抛给调用方。
    """
    return await asyncio.to_thread(input, prompt)


def _raw_sigint_handler(signum: int, frame: FrameType | None):
    """跨平台 SIGINT 处理器：取消 asyncio 任务 + 通知 agent 实例"""
    # 方式 1（Unix 主线程）：直接取消所有 asyncio 任务
    try:
        for task in asyncio.all_tasks():
            task.cancel()
    except (RuntimeError, ValueError):
        pass

    # 方式 2（跨平台回退）：通知实例
    # Windows 上 Ctrl+C 在独立线程运行，asyncio.all_tasks().cancel()
    # 可能不立即生效，portal.run.cancel() 设置实例级中断标记作为安全网，
    # _check_interrupted() 会在下一个循环检查点检测到它。
    if portal.is_open:
        portal.run.cancel()


def _raw_sigterm_handler(signum: int, frame: FrameType | None):
    """SIGTERM 处理器：软中断（仅设置 agent 中断标记，不 cancel asyncio 任务）。

    与 SIGINT 不同：subagent 超时时父进程先 terminate() 发 SIGTERM，
    若像 SIGINT 一样 cancel 所有任务，main task 会被标记取消，
    导致 try/finally 里的 `await agent.shutdown()` 立即抛 CancelledError，
    无法执行 save_and_summarize 写摘要。
    软中断让 process 在 _check_interrupted() 检查点优雅退出，
    finally → shutdown 可完整执行。
    """
    if portal.is_open:
        portal.run.cancel()


async def _send_single(message: str) -> None:
    """单次消息：发一条并回显（-m 与 --headless 组合共用通路）"""
    if os.environ.get("FP_SUBAGENT_SILENT"):
        response = await portal.run.send(message)
        print(response.content, end="")
    else:
        print(f"> {message}")
        response = await portal.run.send(message)
        print(f"\nAgent: {response.content}")


async def _resident_loop() -> None:
    """无头驻留循环（--headless）：不渲染提示符，直到 SIGINT/SIGTERM 才退出。

    与 REPL 的关键差异：REPL 在 `input()` 拿到 EOF（后台/管道、无 pty 启动）
    时 break → 实例当场死亡、zeta 名片失效。驻留模式把「没有输入」视为常态，
    stdin 是否可读、是否已 EOF 都不影响存活——这正是 screen/pty 包裹的替代品。

    信号语义：
    - SIGINT 沿用 main() 装的全局 handler（cancel 全部任务）→ 本协程收到
      CancelledError 后吞掉，交给 main() 的 finally → ctl.close() 优雅收尾
      （与 REPL 里 Ctrl+C 的路径一致）。
    - SIGTERM 全局 handler 只设 agent 中断标记、不 cancel 任务，本循环收不到，
      故在此覆盖为「软中断 + 唤醒 stop」：软中断让进行中的 run 在检查点停下，
      stop 让循环退出，同样落到 finally 收尾。
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_term() -> None:
        if portal.is_open:
            portal.run.cancel()
        stop.set()

    try:
        loop.add_signal_handler(signal.SIGTERM, _on_term)
    except (NotImplementedError, RuntimeError):  # 无信号队列的平台回退

        def _on_term_signal(signum: int, frame: FrameType | None) -> None:
            del signum, frame
            _on_term()

        signal.signal(signal.SIGTERM, _on_term_signal)

    display.info(
        f"{tui_glyph('headless')}  无头驻留中（session={portal.run.status.session_id}），"
        "邻居铃声/注入事件照常处理，Ctrl+C 或 SIGTERM 退出"
    )
    # SIGINT 全局 handler 取消任务 → 视为退出请求，静默交给 finally 收尾
    with contextlib.suppress(asyncio.CancelledError):
        await stop.wait()


async def main():
    """主入口"""
    import argparse

    parser = argparse.ArgumentParser(description="FP - AI Agent 命令行界面")
    parser.add_argument("-m", "--message", help="单次消息模式")
    parser.add_argument(
        "-r", "--resume", nargs="?", const="auto", default=None, metavar="SESSION_ID", help="恢复历史会话"
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="无头驻留模式：跳过交互提示与 logo，常驻直到 SIGINT/SIGTERM（可与 -r 组合）",
    )

    args = parser.parse_args()

    if args.headless:
        # 驻留日志必须实时可见：stdout 重定向到文件时是块缓冲，
        # 攒到退出才落盘——而驻留模式恰恰到收到信号才退出，等于永远看不到。
        # 走 getattr：TextIO 协议没有 reconfigure（StringIO 等实现也没有），
        # 直接调用在 strict 下既过不了类型检查，运行时也未必存在。
        for _stream in (sys.stdout, sys.stderr):
            _reconf = getattr(_stream, "reconfigure", None)
            if callable(_reconf):
                _reconf(line_buffering=True)

    if not config.check_llm_config():
        # 配置缺失：正常经 fp.main 门禁已拦截并给出指引；此处兜底（如直接 python -m fp_cli）
        sys.exit(1)

    # 语义外观装配（config.json 的 tui 段）：样式表与符号表在此发布为进程级快照。
    # REPL 路径必须显式装配——TUI 是在 run_tui() 里装的，而 L1/L2 不走那里；
    # 漏了这步，display/cli_io 的 tui_glyph() 会静默用内置默认（配置不生效）。
    install_appearance()

    # 角色注入（多智能体编排器）：FP_SUBAGENT_ROLE 传 JSON。
    # 实例以 getattr 盲取 role 属性（duck-typed），故用 SimpleNamespace 还原即可，
    # 无需引入核心 AgentRole 类依赖。
    _role: Any = None
    _role_json = os.environ.get("FP_SUBAGENT_ROLE")
    if _role_json:
        import json as _json
        from types import SimpleNamespace as _SimpleNamespace

        try:
            _role = _SimpleNamespace(**_json.loads(_role_json))
        except (ValueError, TypeError):
            _role = None

    # subagent 子进程：使用父进程预生成的会话 ID（父进程据此兜底补写 meta）
    _sub_sid = os.environ.get("FP_SUBAGENT_SID") or None

    # ── 前端模式决策（L0 TUI / L1 经典 REPL）── 必须在 ctl.open(io=) 之前 ──
    # L2（headless/-m/管道）不进交互前端，走 CLIIO 打印通路。
    _interactive = not args.headless and not args.message
    _use_tui, _tui_reason = tui_available() if _interactive else (False, "非交互模式")
    _io: IOChannel = TUIIO() if _use_tui else CLIIO()
    if _use_tui:
        # core 日志改走 TUI 消息流——裸 print 会砸 TUI 屏
        from .tui import TUILogger

        set_logger(TUILogger(_io))  # type: ignore[arg-type]

    await portal.ctl.open(
        resume=args.resume or os.environ.get("FP_RELOAD_SID") or None,
        session_id=_sub_sid,
        io=_io,
        role=_role,
        on_shutdown=display.shutdown_panel,
    )
    if _interactive and not _use_tui:
        # L1 降级可见性：用户应知道当前不在 TUI（排障第一现场）
        display.hint(f"{tui_glyph('paused')} 经典 REPL（{_tui_reason}）")

    # subagent 标记：子进程自身先写 source + parent_sid（父进程会在结束时兜底）
    if os.environ.get("FP_IS_SUBAGENT") == "1":
        _parent_sid = os.environ.get("FP_SUBAGENT_PARENT_SID") or ""
        _sub_meta: dict[str, Any] = {"source": "subagent"}
        if _parent_sid:
            _sub_meta["parent_sid"] = _parent_sid
        # create=False：只标记内存 meta，随首条消息一起落盘；
        # 没产出内容的子会话不该留下 0 长度会话文件
        portal.ctl.sessions.update_meta(create=False, **_sub_meta)

    # ── 安装 SIGINT 处理器 ────────────────────────────────
    #
    # 使用 signal.signal() 而不是 loop.add_signal_handler()
    # 原因：add_signal_handler 依赖 event loop 的 wakeup fd 机制，
    #   在你这个终端环境下信号无法通过该链路抵达处理器。
    # signal.signal() 安装纯 C 级处理器，信号到达时直接触发，
    #   通过 asyncio.all_tasks().cancel() 注入 CancelledError。
    # agent._stream_chat 的 try/except 负责优雅捕获中断。
    signal.signal(signal.SIGINT, _raw_sigint_handler)
    # SIGTERM 同样走优雅取消：父进程 subagent 超时先 terminate()，
    # 让子进程有机会走 finally → shutdown → save_and_summarize 写摘要。
    # 使用专用软中断 handler（不 cancel task，否则 finally 里的 await 会中断）。
    # 平台语义（#170）：Windows 能装 SIGTERM 处理器，但系统从不投递该信号
    # （无 POSIX 信号模型）——那边靠 Ctrl+C 走 SIGINT；安装本身不报错，故无需分支。
    signal.signal(signal.SIGTERM, _raw_sigterm_handler)

    # headless 无交互语境，logo 只会污染驻留日志；TUI 有自己的欢迎屏
    if not os.environ.get("FP_SUBAGENT_QUIET") and not args.headless and not _use_tui:
        display.print_logo(model=portal.run.status.model, resume=args.resume)

    try:
        # ── reload handoff 续接：新实例注入 tool 返回并自动继续对话 ──
        # 机制契约见 fp_core.core.handoff；前端侧只消费 ReloadDirective。
        # 续接取代本轮 -m 消息处理（那条消息已在旧实例中执行并触发了 reload，不可重放）。
        _directive = portal.ctl.take_reload()

        if _use_tui:
            # TUI：directive 交由应用 on_mount 处理（notice/续接渲染进消息流），
            # -m/headless 不会走到这里（_interactive 已过滤）。
            assert isinstance(_io, TUIIO)
            await run_tui(_io, directive=_directive, resume=args.resume)
            return

        if _directive.notice:
            display.divider()
            display.info(_directive.notice)
        _reloaded = _directive.should_continue
        if _reloaded:
            display.divider()
            display.info(f"{tui_glyph('reload')} reload 续接：已恢复会话，继续上一轮对话…")
            print()
            resp = await portal.run.continue_()
            if os.environ.get("FP_SUBAGENT_SILENT") and resp.content:
                print(resp.content, end="")

        if args.message and not _reloaded:
            await _send_single(args.message)

        if args.headless:
            # 无头驻留：无论有无 -m，都常驻等事件（而非像 REPL 那样读到 EOF 就退）
            await _resident_loop()
        elif not args.message:
            # 无 -m：进入 L1 经典 REPL（reload 续接完成后同样回到这里等输入）
            # prompt_toolkit 已退场：裸 input（executor 不阻塞事件循环），无补全/历史
            if args.resume:
                display.hint(f"{tui_glyph('hint')} 续会话: {portal.run.status.session_id}，输入 /help 查看命令")
            else:
                display.hint(f"{tui_glyph('hint')} 输入 /help 查看命令，/resume 可回到历史会话")
            print()

            try:
                line_open = False  # 是否有未配对的"上线"（空输入时不重画）
                while True:
                    if not line_open:
                        display.divider()  # 输入块上方青色隔离线
                        line_open = True

                    try:
                        user_input = await _read_line(f"{tui_glyph('prompt')} ")
                    except (EOFError, KeyboardInterrupt):
                        print()
                        break

                    if not user_input.strip():
                        continue

                    print()  # 换行，与输入行分隔
                    display.divider()  # 输入块下方青色隔离线
                    line_open = False

                    try:
                        response = await portal.run.send(user_input)

                        # 命令输出：由 response.content 单一通路传递，不再由命令内部 display
                        # 此处用 rich Markdown 渲染（terminal 唯一消费点）
                        if user_input.strip().startswith("/") and response.content:
                            try:
                                from rich.console import Console
                                from rich.markdown import Markdown

                                Console().print(Markdown(response.content))
                            except ImportError:
                                print(response.content)
                    except (SystemExit, asyncio.CancelledError):
                        break
                    except Exception as e:
                        display.error(f"错误: {e}")
                        print()
            except KeyboardInterrupt:
                print()
    finally:
        # 保证关闭一定执行：subagent 子进程无论何种退出路径
        # 都能走到 save_and_summarize（生成摘要 + 保存上下文）
        await portal.ctl.close()


if __name__ == "__main__":
    asyncio.run(main())
