"""
命令行交互界面
"""

import asyncio
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from prompt_toolkit.completion import Completer as PtCompleter

import config
import display


class SlashCompleter(PtCompleter):
    """自定义补全器：仅在输入 "/" 前缀时匹配命令和工具名。

    补全字典初始化时从 tools/commands/skills 系统动态加载，确保始终同步。
    每个补全项附带描述信息作为 display_meta，帮助用户快速了解功能。
    """

    def __init__(self):
        self._words: list[str] = []
        self._words_meta: dict[str, str] = {}  # word → description
        self._load_words()

    def _load_words(self):
        """从 tools、commands、skills 系统加载补全词条及描述"""
        words: set[str] = set()
        meta: dict[str, str] = {}

        # 1. 命令名（带 / 前缀）
        try:
            from commands import get_all_commands

            for cmd_name, desc in get_all_commands().items():
                word = f"/{cmd_name}"
                words.add(word)
                if desc:
                    meta[word] = desc
        except Exception:
            pass

        self._words = sorted(words, key=lambda w: (not w.startswith("/"), w))
        self._words_meta = meta

    def get_completions(self, document, complete_event):
        """prompt_toolkit Completer 接口"""
        from prompt_toolkit.completion import Completion

        text = document.text_before_cursor

        # 仅在输入以 "/" 开头时才触发补全
        if not text.startswith("/"):
            return

        for word in self._words:
            if word.startswith(text):
                desc = self._words_meta.get(word, "")
                yield Completion(
                    word,
                    start_position=-len(text),
                    display_meta=desc,
                )

    async def get_completions_async(self, document, complete_event):
        """prompt_toolkit 要求的异步补全接口"""
        for completion in self.get_completions(document, complete_event):
            yield completion


class InputHandler:
    """交互式输入（prompt_toolkit 封装，支持历史/斜杠补全）"""

    def __init__(self, prompt: str = "(Agent) > "):
        self.prompt = prompt
        self._session = None
        self._init_session()

    def _build_key_bindings(self):
        """自定义键绑定：Tab 确认补全（而非循环选择下一个）"""
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.keys import Keys

        kb = KeyBindings()

        @kb.add(Keys.Tab)
        def _(event):
            """Tab 键：确认当前选中的补全项，无选中时直接触发补全"""
            b = event.current_buffer

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

            history_file = os.path.join(config.MEMORY_DIR, "_input_history")
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
        return input(self.prompt)


def _raw_sigint_handler(signum, frame):
    """跨平台 SIGINT 处理器：取消 asyncio 任务 + 设置全局标志"""
    # 方式 1（Unix 主线程）：直接取消所有 asyncio 任务
    # 在 Unix 上，signal handler 在主线程运行，all_tasks() 可用
    try:
        for task in asyncio.all_tasks():
            task.cancel()
    except (RuntimeError, ValueError):
        pass  # 无运行中的事件循环

    # 方式 2（跨平台回退）：设置全局标志
    # Windows 上 Ctrl+C 在独立线程运行，all_tasks() 可能失败，
    # 此标志作为安全网，_check_interrupted() 会检查它
    try:
        import core.agent as _agent_mod

        _agent_mod._interrupted_flag = True
    except Exception:
        pass


async def main():
    """主入口"""
    import argparse

    parser = argparse.ArgumentParser(description="五块卵石 - AI Agent 命令行界面")
    parser.add_argument("-m", "--message", help="单次消息模式")
    parser.add_argument(
        "-r", "--resume", nargs="?", const="auto", default=None, metavar="SESSION_ID", help="恢复历史会话"
    )
    parser.add_argument("--init", action="store_true", help="初始化配置文件")

    args = parser.parse_args()

    if args.init:
        config.init_config()
        return

    if not config.check_llm_config():
        return

    from core.agent import Agent

    agent = Agent(resume=args.resume)

    # ── 安装 SIGINT 处理器 ────────────────────────────────
    #
    # 使用 signal.signal() 而不是 loop.add_signal_handler()
    # 原因：add_signal_handler 依赖 event loop 的 wakeup fd 机制，
    #   在你这个终端环境下信号无法通过该链路抵达处理器。
    # signal.signal() 安装纯 C 级处理器，信号到达时直接触发，
    #   通过 asyncio.all_tasks().cancel() 注入 CancelledError。
    # agent._stream_chat 的 try/except 负责优雅捕获中断。
    signal.signal(signal.SIGINT, _raw_sigint_handler)

    if not os.environ.get("FP_SUBAGENT_QUIET"):
        display.print_logo()
        display.startup(agent.model, resume=args.resume)

    if args.message:
        print(f"> {args.message}")
        response = await agent.process(args.message)
        print(f"\nAgent: {response.content}")
    else:
        inp = InputHandler()

        if args.resume:
            display.hint(f"💡 续会话: {agent.session.session_id}，输入 /help 查看命令")
        else:
            display.hint("💡 输入 /help 查看命令，/resume 可回到历史会话")
        print()

        try:
            while True:
                try:
                    user_input = await inp.prompt_async()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break

                if not user_input.strip():
                    continue

                try:
                    response = await agent.process(user_input)
                except (SystemExit, asyncio.CancelledError):
                    break
                except Exception as e:
                    display.error(f"错误: {e}")
                    print()
        except KeyboardInterrupt:
            print()

    await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
