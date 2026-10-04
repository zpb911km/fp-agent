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

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.completion import Completer as PtCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyPressEvent

from fp_core import config
from fp_core.api import portal
from fp_core.logger import Logger, set_logger

from . import display
from .cli_io import CLIIO


# ── Terminal Logger：fp-core 内部日志 → 终端输出 ──
class _TerminalLogger(Logger):
    def info(self, msg: str):
        display.info(msg)

    def warning(self, msg: str):
        display.warning(msg)

    def error(self, msg: str):
        display.error(msg)


set_logger(_TerminalLogger())


class SlashCompleter(PtCompleter):
    """自定义补全器：仅在输入 "/" 前缀时匹配命令和工具名。

    补全字典从 tools/commands 系统动态加载，并在每次补全前重载，
    确保始终同步（/reload 或插件热启停注入的命令能立即出现）。
    每个补全项附带描述信息作为 display_meta，帮助用户快速了解功能。
    """

    def __init__(self):
        self._words: list[str] = []
        self._words_meta: dict[str, str] = {}  # word → description
        self._load_words()

    def _load_words(self):
        """从 tools、commands 系统加载补全词条及描述"""
        words: set[str] = set()
        meta: dict[str, str] = {}

        # 1. 命令名（带 / 前缀）
        try:
            for cmd_name, desc in portal.run.commands.items():
                word = f"/{cmd_name}"
                words.add(word)
                if desc:
                    meta[word] = desc
        except Exception:
            pass

        self._words = sorted(words, key=lambda w: (not w.startswith("/"), w))
        self._words_meta = meta

    def get_completions(self, document: Document, complete_event: CompleteEvent):
        """prompt_toolkit Completer 接口"""
        from prompt_toolkit.completion import Completion

        text = document.text_before_cursor

        # 仅在输入以 "/" 开头时才触发补全
        if not text.startswith("/"):
            return

        # 惰性重载：命令表可能因 /reload 或插件热启停而变化，
        # 每次补全前重新抓取，避免词表停留在 REPL 启动那一刻。
        self._load_words()

        for word in self._words:
            if word.startswith(text):
                desc = self._words_meta.get(word, "")
                yield Completion(
                    word,
                    start_position=-len(text),
                    display_meta=desc,
                )


class InputHandler:
    """交互式输入（prompt_toolkit 封装，支持历史/斜杠补全）"""

    def __init__(self, prompt: str = "(Agent) > "):
        self._plain_prompt = prompt
        self.prompt = self._build_prompt(prompt)
        self._session: PromptSession[str] | None = None
        self._init_session()

    @staticmethod
    def _build_prompt(fallback: str):
        """构建亮青 ❯ 现代 prompt；prompt_toolkit 不可用时回退纯文本"""
        try:
            from prompt_toolkit.formatted_text import HTML

            return HTML("<ansicyan><b>❯ </b></ansicyan>")
        except ImportError:
            return fallback

    def _build_key_bindings(self):
        """自定义键绑定：Tab 确认补全（而非循环选择下一个）"""
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.keys import Keys

        kb = KeyBindings()

        @kb.add(Keys.Tab)
        def _(event: KeyPressEvent):
            """Tab 键：确认当前选中的补全项，无选中时直接触发补全"""
            from prompt_toolkit.buffer import Buffer

            b: Buffer = event.current_buffer

            if b.complete_state is not None:
                # 补全菜单已显示 → 确认当前选中项
                current = b.complete_state.current_completion
                if current is not None:
                    b.apply_completion(current)
                else:
                    # 没有选中具体项时，选中第一个并确认
                    completions = b.complete_state.completions
                    if completions:
                        b.apply_completion(completions[0])
            else:
                # 无补全菜单 → 触发补全
                b.start_completion(select_first=True)

        return kb

    def _init_session(self):
        if not sys.stdin.isatty():
            return
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.history import FileHistory

            history_file = config.INPUT_HISTORY_FILE
            os.makedirs(os.path.dirname(history_file), exist_ok=True)

            # 构建补全器（延迟加载，确保 tools/commands 已就绪）
            completer = SlashCompleter()
            key_bindings = self._build_key_bindings()

            self._session = PromptSession(
                history=FileHistory(history_file),
                completer=completer,
                key_bindings=key_bindings,
                enable_open_in_editor=True,
            )
        except Exception:
            self._session = None

    async def prompt_async(self) -> str:
        if self._session:
            return await self._session.prompt_async(self.prompt)
        prompt_text = self.prompt if isinstance(self.prompt, str) else self._plain_prompt
        return input(prompt_text)


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
        f"🖥  无头驻留中（session={portal.run.status.session_id}），邻居铃声/注入事件照常处理，Ctrl+C 或 SIGTERM 退出"
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
    parser.add_argument("--init", action="store_true", help="初始化配置文件")
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

    if args.init:
        config.init_config()
        return

    if not config.check_llm_config():
        return

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
    await portal.ctl.open(
        resume=args.resume or os.environ.get("FP_RELOAD_SID") or None,
        session_id=_sub_sid,
        io=CLIIO(),
        role=_role,
        on_shutdown=display.shutdown_panel,
    )

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
    signal.signal(signal.SIGTERM, _raw_sigterm_handler)

    # headless 无交互语境，logo 只会污染驻留日志
    if not os.environ.get("FP_SUBAGENT_QUIET") and not args.headless:
        display.print_logo(model=portal.run.status.model, resume=args.resume)

    try:
        # ── reload handoff 续接：新实例注入 tool 返回并自动继续对话 ──
        # 机制契约见 fp_core.core.handoff；前端侧只消费 ReloadDirective。
        # 续接取代本轮 -m 消息处理（那条消息已在旧实例中执行并触发了 reload，不可重放）。
        _directive = portal.ctl.take_reload()
        if _directive.notice:
            display.divider()
            display.info(_directive.notice)
        _reloaded = _directive.should_continue
        if _reloaded:
            display.divider()
            display.info("🔄 reload 续接：已恢复会话，继续上一轮对话…")
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
            # 无 -m：进入 REPL（reload 续接完成后同样回到这里等输入）
            inp = InputHandler()

            if args.resume:
                display.hint(f"💡 续会话: {portal.run.status.session_id}，输入 /help 查看命令")
            else:
                display.hint("💡 输入 /help 查看命令，/resume 可回到历史会话")
            print()

            try:
                line_open = False  # 是否有未配对的"上线"（空输入时不重画）
                while True:
                    if not line_open:
                        display.divider()  # 输入块上方青色隔离线
                        line_open = True

                    try:
                        user_input = await inp.prompt_async()
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
