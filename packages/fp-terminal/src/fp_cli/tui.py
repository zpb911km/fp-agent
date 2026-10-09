"""TUI — Textual 交互界面（L0 主路径）

降级链（``main._tui_available`` 决策）：
    L0 TUI（本模块） → L1 经典 print REPL（裸 input，无补全） → L2 headless/-m/管道

渲染模型：VerticalScroll 消息流 + 常驻输入区（多行 PromptInput + 发送按钮，状态条/补全提示条）。
    - 已固化内容 = 追加的 Static/Markdown 子件（顺序天然正确，历史即日志）
    - 流式中内容 = 单个 live 子件，token 节流（call_later 合批）update，
      段末固化（live 引用置空，子件保留可见以迎接下一段）

输入路由（``route_input`` 纯函数，可独立测试）：
    空 → ignore ；斜杠+空闲 → command（send 完整钩子链）
    斜杠+忙碌 → reject（n3 决策：运行中禁用斜杠，防会话状态竞态）
    普通+忙碌 → queue（submit 环顶穿插）；普通+空闲 → send

忙碌判定 = 本地 ``_sending``（handler 同步置位，防连按双发）or portal 状态
（is_run_active 覆盖整轮含工具期 → 含泵起轮）。core 侧 ``Agent`` 三入口共享
``_round_lock``（send 等锁、wake 见忙让位）；TUI 的 check→send 之间无 await，
单线程下无竞态窗口。

ask（n8）：TUIIO.ask 挂起 Future → AskRequest 消息 → AskModal（选项卡 +
自由输入）→ dismiss 携最终文本 resolve。app 退出时 worker 被取消，Future
连带取消不悬挂；app 未挂载时返回 ""（不阻塞 agent 于不可见的 modal）。
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from typing import TYPE_CHECKING, Any, cast

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.geometry import Offset
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import Button, Input, Markdown, OptionList, Static, TextArea

from fp_core.api import IOChannel, portal
from fp_core.logger import Logger

from . import charts, completion
from .history import append_history, load_history

if TYPE_CHECKING:
    from fp_core.api.types import ReloadDirective


# ══════════════════════════════════════════════════════════════
# 输入路由（纯函数）
# ══════════════════════════════════════════════════════════════


def route_input(text: str, busy: bool) -> str:
    """把一条用户输入路由到动作。

    Returns:
        ``ignore`` / ``command`` / ``reject_slash`` / ``queue`` / ``send``
    """
    if not text.strip():
        return "ignore"
    if text.strip().startswith("/"):
        return "reject_slash" if busy else "command"
    return "queue" if busy else "send"


def _history_path() -> str:
    from fp_core import config

    return config.INPUT_HISTORY_FILE


# ══════════════════════════════════════════════════════════════
# 消息（agent IO → 应用；FIFO 保证顺序）
# ══════════════════════════════════════════════════════════════


class IOMsg(Message):
    """agent IO 输出统一封装：kind ∈ info/warning/error/think/content/finalize/tool/tool_result/status"""

    def __init__(self, kind: str, payload: Any) -> None:
        super().__init__()
        self.kind = kind
        self.payload = payload


class AskRequest(Message):
    """ask_user 请求：携带 Future，modal dismiss 后 resolve"""

    def __init__(
        self,
        prompt: str,
        options: list[str] | None,
        suggest: str,
        future: asyncio.Future[str],
    ) -> None:
        super().__init__()
        self.prompt = prompt
        self.options = options
        self.suggest = suggest
        self.future = future


# ══════════════════════════════════════════════════════════════
# TUIIO — agent 输出 → 消息（挂载前缓冲，FIFO 冲刷）
# ══════════════════════════════════════════════════════════════


class TUIIO(IOChannel):
    """Textual 通道：IO 回调 → IOMsg（节流合批流式 token）。

    app 挂载前（含泵起轮 / reload 续接前的输出）一律进 ``pending`` 缓冲，
    挂载后按 FIFO 一次冲刷——顺序不变量与消息队列一致。
    """

    frontend = "terminal"

    def __init__(self) -> None:
        self._app: FPApp | None = None
        self._pending: list[IOMsg] = []
        # 流式节流缓冲（think/content 分开）
        self._think_buf: list[str] = []
        self._content_buf: list[str] = []
        self._flush_scheduled = False
        # 本轮是否产生过流式输出（send 返回后判断是否需补渲染 response.content）
        self.consumed_stream = False

    # ── 挂载管理 ─────────────────────────────────────────────

    def attach(self, app: FPApp) -> None:
        self._app = app

    @property
    def _ready(self) -> bool:
        app = self._app
        return app is not None and app.mounted

    def take_pending(self) -> list[IOMsg]:
        """挂载时取走缓冲（含未冲刷的流式残段——按 finalize 语义收尾）。"""
        self._flush_buffers(self._pending, final=True)
        out = self._pending
        self._pending = []
        return out

    # ── 发射 ─────────────────────────────────────────────────

    def _emit(self, kind: str, payload: Any) -> None:
        msg = IOMsg(kind, payload)
        if not self._ready:
            self._pending.append(msg)
            return
        assert self._app is not None
        self._app.post_message(msg)

    def _flush_buffers(self, sink: list[IOMsg] | None, *, final: bool = False) -> None:
        """把流式缓冲落为 think/content(+finalize) 消息。"""
        self._flush_scheduled = False
        target = sink if sink is not None else None

        def put(kind: str, payload: Any) -> None:
            msg = IOMsg(kind, payload)
            if target is not None:
                target.append(msg)
            elif self._app is not None and self._app.mounted:
                self._app.post_message(msg)
            else:
                self._pending.append(msg)

        if self._think_buf:
            put("think", "".join(self._think_buf))
            self._think_buf = []
        if self._content_buf:
            put("content", "".join(self._content_buf))
            self._content_buf = []
            if final:
                put("finalize", None)

    def _flush_now(self) -> None:
        self._flush_buffers(None)

    def _schedule_flush(self) -> None:
        if self._flush_scheduled or not self._ready:
            return
        assert self._app is not None
        self._flush_scheduled = True
        self._app.call_later(self._timed_flush)

    def _timed_flush(self) -> None:
        self._flush_now()

    # ── IOChannel 契约 ───────────────────────────────────────

    def info(self, text: str) -> None:
        self._emit("info", text)

    def warning(self, text: str) -> None:
        self._emit("warning", text)

    def error(self, text: str) -> None:
        self._emit("error", text)

    async def thinking_start(self, streaming: bool = False) -> None:
        if not streaming:
            self._emit("status", "💭 思考中…")

    async def thinking_stop(self) -> None:
        self._emit("status", "")

    def stream_think(self, token: str) -> None:
        self.consumed_stream = True
        self._think_buf.append(token)
        self._schedule_flush()

    def stream_write(self, content: str) -> None:
        self.consumed_stream = True
        self._content_buf.append(content)
        self._schedule_flush()

    def stream_end(self) -> None:
        self._flush_now()
        self._emit("finalize", None)

    def stream_reset(self) -> None:
        self._think_buf = []
        self._content_buf = []
        self._flush_scheduled = False

    def tool_call(self, name: str, args: dict[str, Any]) -> None:
        self._flush_now()  # 先落盘在途流段，再插工具行（顺序不变量）
        try:
            rendered = json.dumps(args, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            rendered = str(args)
        if len(rendered) > 160:
            rendered = rendered[:160] + "…"
        self._emit("tool", f"{name}({rendered})")

    def tool_result(self, result: str) -> None:
        self._flush_now()
        lines = result.splitlines() or [""]
        head = "\n".join(lines[:6])
        if len(lines) > 6:
            head += f"\n… （共 {len(lines)} 行）"
        self._emit("tool_result", head)

    async def ask(
        self,
        prompt: str,
        *,
        options: list[str] | None = None,
        suggest: str = "",
        ask_id: str | None = None,
    ) -> str:
        del ask_id  # 带内应答走 portal.reply，默认实现不依赖
        app = self._app
        if app is None or not app.mounted:
            return ""  # 挂载前无可见 UI：按"未回答"降级，不悬挂 agent
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        app.post_message(AskRequest(prompt, options, suggest, future))
        return await future


# ══════════════════════════════════════════════════════════════
# TUILogger — fp-core 日志 → TUI 消息流（防裸 print 砸屏）
# ══════════════════════════════════════════════════════════════


class TUILogger(Logger):
    """core logger 接 TUI：经 TUIIO 进消息流（挂载前缓冲，不丢）。"""

    def __init__(self, io: TUIIO) -> None:
        self._io = io

    def info(self, msg: str) -> None:
        self._io.info(msg)

    def warning(self, msg: str) -> None:
        self._io.warning(msg)

    def error(self, msg: str) -> None:
        self._io.error(msg)


# ══════════════════════════════════════════════════════════════
# AskModal（n8）— 询问选项卡
# ══════════════════════════════════════════════════════════════


class AskModal(ModalScreen[str]):
    """结构化问答：选项卡（OptionList）+ 自由输入。返回最终文本。

    解析语义与 CLI 一致：空回车 → 推荐值（无推荐 → ""）；
    编号 → 选项原文；其余 → 原文。Esc → ""（未回答）。
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, prompt: str, options: list[str] | None, suggest: str) -> None:
        super().__init__()
        self._prompt = prompt
        self._options = options or []
        self._suggest = suggest

    def compose(self) -> ComposeResult:
        with Container(id="ask_box"):
            yield Static(f"❓ {self._prompt}", id="ask_prompt")
            if self._options:
                yield OptionList(*self._options, id="ask_opts")
            hint = "Enter 提交 · Esc 跳过"
            if self._suggest:
                hint = f"💡 推荐默认值：{self._suggest}（空回车采纳） · {hint}"
            yield Static(hint, id="ask_hint")
            yield Input(placeholder="输入编号或自由文本…", id="ask_input")

    def on_mount(self) -> None:
        if self._options:
            self.query_one("#ask_opts", OptionList).focus()
        else:
            self.query_one("#ask_input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss("")

    def _parse(self, raw: str) -> str:
        raw = raw.strip()
        if not raw:
            return self._suggest
        if self._options and raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(self._options):
                return self._options[idx - 1]
        return raw

    @on(OptionList.OptionSelected, "#ask_opts")
    def _on_option(self, event: OptionList.OptionSelected) -> None:
        raw: object = event.option.prompt
        self.dismiss(raw if isinstance(raw, str) else str(raw))

    @on(Input.Submitted, "#ask_input")
    def _on_input(self, event: Input.Submitted) -> None:
        self.dismiss(self._parse(event.value))


# ══════════════════════════════════════════════════════════════
# StatsModal（n9）— 图表位
# ══════════════════════════════════════════════════════════════


class StatsModal(ModalScreen[None]):
    """会话统计图表（Ctrl+G）：角色分布条形图 + 近 30 条长度 sparkline。"""

    BINDINGS = [
        Binding("escape", "close", show=False),
        Binding("enter", "close", show=False),
    ]

    def __init__(self, body: Text) -> None:
        super().__init__()
        self._body = body

    def compose(self) -> ComposeResult:
        with Container(id="stats_box"):
            yield Static("📊 会话统计", id="stats_title")
            yield Static(self._body, id="stats_body")
            yield Static("Enter/Esc 关闭", id="stats_hint")

    def action_close(self) -> None:
        self.dismiss(None)


def build_stats_body() -> Text:
    """从会话转写构建统计文本（portal 只读面，不触内部）。"""
    text = Text()
    try:
        msgs = portal.run.transcript if portal.is_open else []
    except Exception:  # noqa: BLE001 — 统计面板不得因读取失败崩 UI
        msgs = []
    counts: Counter[str] = Counter(str(m.get("role", "?")) for m in msgs)
    for row in charts.bar_rows(dict(counts)):
        text.append(row + "\n")
    lens = [len(str(m.get("content") or "")) for m in msgs if m.get("role") != "system"][-30:]
    text.append("\n近 30 条消息长度\n")
    text.append(charts.spark_segments(lens) or "（数据不足）", style="cyan")
    text.append(f"\n共 {len(msgs)} 条消息")
    return text


# ══════════════════════════════════════════════════════════════
# FPApp — 主应用
# ══════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════
# FPDefaultScreen — 上游 #6643 竞态的本地等价修复（PR #6645）
# ══════════════════════════════════════════════════════════════


class FPDefaultScreen(Screen[None]):
    """默认屏：已 detach 的命中视为 miss。

    ``Markdown.update()`` 会摘换旧块，而 compositor 命中图可能仍指向
    旧块；``Screen._forward_event`` 拿到 ``parent is None`` 的块会崩
    （``AttributeError: 'NoneType' has no attribute 'region'``，上游
    Textualize/textual#6643）。等价修法见上游 PR #6645（尚未合并）：
    ``not widget.is_attached`` → miss。上游发布修复版本后本类可退场。
    """

    def get_widget_and_offset_at(self, x: int, y: int) -> tuple[Widget | None, Offset | None]:
        widget, offset = super().get_widget_and_offset_at(x, y)
        if widget is not None and not widget.is_attached:
            return None, None
        return widget, offset


class PromptInput(TextArea):
    """多行输入框：Enter 换行（TextArea 原生），Shift+Enter 发送（App 优先级绑定）。

    ↑↓ 语义按内容分流（确定性，不依赖按键冒泡）：
    - 有正文 → 光标移动（多行编辑）；
    - 空正文 → 历史回填（沿袭单行 Input 时代的 ↑↓ 行为）。
    """

    BINDINGS = [
        Binding("up", "prompt_up", show=False),
        Binding("down", "prompt_down", show=False),
    ]

    def action_prompt_up(self) -> None:
        if self.text.strip():
            self.action_cursor_up()
        else:
            # self.app 的返回类型带 Unknown（textual Widget.app 泛型），strict 下单点豁免
            cast("FPApp", self.app).action_hist_prev()  # pyright: ignore[reportUnknownMemberType]

    def action_prompt_down(self) -> None:
        if self.text.strip():
            self.action_cursor_down()
        else:
            cast("FPApp", self.app).action_hist_next()  # pyright: ignore[reportUnknownMemberType]


# ══════════════════════════════════════════════════════════════
# FPApp — 主界面
# ══════════════════════════════════════════════════════════════


class FPApp(App[None]):
    """FP 终端主界面。

    生命周期：``run_tui()`` 创建 → attach(io) → run_async() → on_mount 冲刷
    pending / 渲染欢迎与 reload 续接 → 事件循环。退出后 main() finally 收尾。
    """

    CSS = """
    Screen { layout: vertical; }
    #status {
        dock: top; height: 1;
        background: $surface; color: $text;
        padding: 0 1;
    }
    #chat { height: 1fr; padding: 0 1; }
    #hint {
        height: auto; max-height: 9;
        background: $panel; color: $text;
        padding: 0 1; display: none;
    }
    #hint.visible { display: block; }
    #prompt_row {
        dock: bottom;
        height: auto;
        background: $panel;
        border-top: solid $accent;
        padding: 0 1;
    }
    #prompt {
        width: 1fr;
        height: 4;
        max-height: 8;
        background: $panel;
        color: $text;
    }
    #send_btn {
        width: 10;
        height: 3;
        margin: 1 0 1 1;
        background: $accent;
        color: $text;
    }
    #ask_box, #stats_box {
        width: 80%; max-width: 100; height: auto;
        max-height: 90%;
        border: round $accent; background: $panel;
        padding: 1 2; margin: 2 1;
    }
    #ask_prompt, #stats_title { text-style: bold; color: $accent; }
    #ask_opts { max-height: 14; }
    #ask_hint, #stats_hint { color: $text-muted; }
    AskModal, StatsModal { align: center middle; background: $background 60%; }
    """

    BINDINGS = [
        Binding("ctrl+c", "interrupt", "打断/退出", priority=True),
        Binding("ctrl+q", "app_quit", "退出", priority=True),
        Binding("ctrl+g", "stats", "统计"),
        Binding("shift+enter", "submit_prompt", show=False, priority=True),
        Binding("tab", "tab_complete", show=False, priority=True),
    ]

    def __init__(
        self,
        io: TUIIO,
        *,
        directive: ReloadDirective | None = None,
        resume: str | None = None,
    ) -> None:
        super().__init__()
        self._io = io
        self._directive = directive
        self._resume = resume
        # 挂载状态：决定 IO 输出走消息还是缓冲
        self.mounted = False
        # 本地发送标志（handler 同步置位，防连按双发）
        self._sending = False
        # 流式 live 子件引用（None = 当前无在途流段）
        self._live: Markdown | Static | None = None
        self._live_kind: str | None = None  # "think" | "content"
        self._live_buf = ""
        # 状态附注（spinner 文本）
        self._status_note = ""
        # 输入历史
        self._hist: list[str] = []
        self._hist_idx = 0
        self._hint_matches: list[tuple[str, str]] = []

    def get_default_screen(self) -> Screen[None]:
        """默认屏换用带 #6643 竞态修复的 FPDefaultScreen。"""
        return FPDefaultScreen(id="_default")

    # ── 组装 ─────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with VerticalScroll(id="chat"):
            yield Static("", id="chat_anchor")
        yield Static(id="hint")
        with Horizontal(id="prompt_row"):
            yield PromptInput(
                placeholder="输入消息…（Enter 换行 · Shift+Enter 发送 · Tab 补全斜杠命令）",
                id="prompt",
            )
            yield Button("发送", id="send_btn", variant="primary")

    async def on_mount(self) -> None:
        self.mounted = True
        self._hist = load_history(_history_path())
        self._hist_idx = len(self._hist)
        # 挂载前缓冲的 agent 输出（含泵起轮残段）→ FIFO 冲刷
        for msg in self._io.take_pending():
            self._handle_io(msg)
        # 欢迎 + reload 续接
        self._render_welcome()
        if self._directive is not None and self._directive.should_continue:
            self.run_worker(self._continue_round())
        elif self._directive is not None and self._directive.notice:
            self._line(self._directive.notice, "cyan")
        # 状态刷新（覆盖泵起轮的忙碌态——它们不经本应用）
        self.set_interval(0.4, self._refresh_status)
        self._refresh_status()
        self.query_one("#prompt", PromptInput).focus()

    async def _continue_round(self) -> None:
        if self._directive is not None and self._directive.notice:
            self._line(self._directive.notice, "cyan")
        self._line("🔄 reload 续接：已恢复会话，继续上一轮对话…", "cyan")
        try:
            await portal.run.continue_()
        except (SystemExit, asyncio.CancelledError):
            raise
        except Exception as e:  # noqa: BLE001 — 续接失败展示而非崩溃
            self._line(f"续接失败: {e}", "red")

    def _render_welcome(self) -> None:
        try:
            st = portal.run.status
            model, sid = st.model, st.session_id
        except Exception:  # noqa: BLE001
            model, sid = "?", "?"
        head = Text()
        head.append("🤖 FP · ", style="bold")
        head.append(model, style="cyan")
        head.append(" · ", style="dim")
        head.append(sid, style="dim")
        self._mount_block(head)
        if self._resume:
            self._line(f"💡 续会话: {sid}，输入 /help 查看命令", "cyan")
        else:
            self._line("💡 输入 /help 查看命令，/resume 可回到历史会话", "cyan")
        self._line(
            "⌨ Enter 换行 · Shift+Enter/发送钮 提交 · Ctrl+C 打断/退出 · Ctrl+G 统计 · 运行中输入排队注入",
            "dim",
        )

    # ── 消息流渲染 ───────────────────────────────────────────

    def _chat(self) -> VerticalScroll:
        return self.query_one("#chat", VerticalScroll)

    def _at_bottom(self) -> bool:
        chat = self._chat()
        return chat.scroll_offset.y >= chat.max_scroll_y - 3

    def _follow_scroll(self, was_at_bottom: bool) -> None:
        if was_at_bottom:
            self._chat().scroll_end(animate=False)

    def _mount_block(self, renderable: Any) -> None:
        """追加一个固化块到消息流尾部（已是 Widget 则直接挂，不再包 Static）。"""
        from textual.widget import Widget

        at_bottom = self._at_bottom()
        if isinstance(renderable, Widget):
            self._chat().mount(renderable)
        else:
            self._chat().mount(Static(renderable))
        self._follow_scroll(at_bottom)

    def _line(self, text: str, style: str = "") -> None:
        self._mount_block(Text(text, style=style) if style else Text(text))

    def _finalize_live(self) -> None:
        """固化当前流段（子件保留可见，引用置空以迎接下一段）。"""
        self._live = None
        self._live_kind = None
        self._live_buf = ""

    def _feed_live(self, kind: str, chunk: str) -> None:
        at_bottom = self._at_bottom()
        if self._live is None or self._live_kind != kind:
            self._finalize_live()
            self._live_kind = kind
            self._live_buf = chunk
            widget: Markdown | Static = Markdown(chunk) if kind == "content" else Static(Text(chunk, style="dim"))
            self._chat().mount(widget)
            self._live = widget
        else:
            self._live_buf += chunk
            if self._live_kind == "content":
                md = self._live
                assert isinstance(md, Markdown)
                md.update(self._live_buf)
            else:
                st = self._live
                assert isinstance(st, Static)
                st.update(Text(self._live_buf, style="dim"))
        self._follow_scroll(at_bottom)

    # ── IO 分发 ──────────────────────────────────────────────

    @on(IOMsg)
    def _on_io(self, msg: IOMsg) -> None:
        self._handle_io(msg)

    def _handle_io(self, msg: IOMsg) -> None:
        kind, payload = msg.kind, msg.payload
        if kind == "info":
            self._line(str(payload), "cyan")
        elif kind == "warning":
            self._line(str(payload), "yellow")
        elif kind == "error":
            self._line(str(payload), "red")
        elif kind == "tool":
            self._mount_block(Text("  ⚙ ", style="magenta") + Text(str(payload), style="magenta"))
        elif kind == "tool_result":
            self._line(str(payload), "dim")
        elif kind == "think":
            if self._live_kind == "content":
                self._finalize_live()
            self._feed_live("think", str(payload))
        elif kind == "content":
            self._feed_live("content", str(payload))
        elif kind == "finalize":
            self._finalize_live()
        elif kind == "status":
            self._status_note = str(payload)
            self._refresh_status()

    # ── ask（n8） ────────────────────────────────────────────

    @on(AskRequest)
    def _on_ask(self, req: AskRequest) -> None:
        def _resolve(value: str | None) -> None:
            if not req.future.done():
                req.future.set_result(value or "")

        self.push_screen(AskModal(req.prompt, req.options, req.suggest), _resolve)

    # ── 状态条 ───────────────────────────────────────────────

    def _is_busy(self) -> bool:
        if self._sending:
            return True
        if not portal.is_open:
            return False
        try:
            st = portal.run.status
            return st.is_run_active or st.is_processing
        except Exception:  # noqa: BLE001
            return False

    def _refresh_status(self) -> None:
        bar = self.query_one("#status", Static)
        try:
            st = portal.run.status if portal.is_open else None
        except Exception:  # noqa: BLE001
            st = None
        busy = self._is_busy()
        t = Text()
        if st is not None:
            t.append(st.model, style="bold cyan")
            t.append(" · ")
            t.append(st.session_id[:8], style="dim")
            t.append(" · ")
        t.append("🔴 运行中" if busy else "🟢 空闲", style="bold" if busy else "green")
        if self._status_note:
            t.append(f" · {self._status_note}", style="yellow")
        bar.update(t)
        prompt = self.query_one("#prompt", PromptInput)
        prompt.placeholder = (
            "运行中 — 输入将作为补充排队注入（斜杠命令已禁用）"
            if busy
            else "输入消息… Shift+Enter 发送 · Enter 换行 · Tab 补全斜杠命令"
        )

    # ── 输入路由 ─────────────────────────────────────────────

    def action_submit_prompt(self) -> None:
        """Shift+Enter / 发送按钮 → 取文提交（modal 打开时不抢）。"""
        if isinstance(self.screen, ModalScreen):
            return
        prompt = self.query_one("#prompt", PromptInput)
        text = prompt.text
        prompt.clear()
        self.run_worker(self._handle_input(text))

    @on(Button.Pressed, "#send_btn")
    def _on_send_click(self, event: Button.Pressed) -> None:
        # 点击会把焦点移到按钮——夺回输入焦点，保证后续键入不丢失
        self.query_one("#prompt", PromptInput).focus()
        self.action_submit_prompt()

    async def _handle_input(self, text: str) -> None:
        action = route_input(text, self._is_busy())
        if action == "ignore":
            return
        append_history(_history_path(), text)
        self._hist.append(text)
        self._hist_idx = len(self._hist)

        if action == "reject_slash":
            self._line("⛔ 运行中已禁用斜杠命令（本轮结束后可用，Ctrl+C 可打断）", "yellow")
            return
        if action == "queue":
            self._mount_block(Text(f"❯ {text}  ⏳已排队", style="bold cyan"))
            try:
                result = await portal.run.submit(text)
            except Exception as e:  # noqa: BLE001
                self._line(f"排队失败: {e}", "red")
                return
            if result == "rejected_command":  # 理论不可达（上面已拦）
                self._line("⛔ 斜杠命令不入队", "yellow")
            elif result != "queued":
                self._line(f"排队结果: {result}", "yellow")
            return

        # command / send：本地发送标志同步置位（防连按双发），轮锁兜底泵交错
        self._mount_block(Text(f"❯ {text}", style="bold cyan"))
        self._sending = True
        self._io.consumed_stream = False
        self.run_worker(self._do_round(text, command=action == "command"))

    async def _do_round(self, text: str, *, command: bool) -> None:
        resp: Any = None
        try:
            resp = await portal.run.send(text)
        except SystemExit as e:
            if str(e):
                self._line(str(e), "yellow")
            self.exit()
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 单轮失败展示而非崩溃
            self._line(f"错误: {e}", "red")
            return
        finally:
            self._sending = False
            self._refresh_status()
        if resp is not None and resp.content and (command or not self._io.consumed_stream):
            # 斜杠命令输出 / 早退响应（blocked、过滤等未走流式）→ 补渲染
            self._finalize_live()
            self._mount_block(Markdown(resp.content))
        self._refresh_status()

    # ── 斜杠补全提示（n7） ───────────────────────────────────

    @on(TextArea.Changed, "#prompt")
    def _on_change(self, event: TextArea.Changed) -> None:
        hint = self.query_one("#hint", Static)
        value = event.text_area.text
        if not value.startswith("/"):
            self._hint_matches = []
            hint.remove_class("visible")
            hint.update("")
            return
        self._hint_matches = completion.slash_matches(value)[:8]
        if not self._hint_matches:
            hint.remove_class("visible")
            hint.update("")
            return
        body = Text()
        for word, desc in self._hint_matches:
            body.append(word, style="bold cyan")
            if desc:
                body.append(f"  {desc}", style="dim")
            body.append("\n")
        hint.update(body)
        hint.add_class("visible")

    def action_tab_complete(self) -> None:
        if not self._hint_matches:
            return
        prompt = self.query_one("#prompt", PromptInput)
        word = self._hint_matches[0][0]
        prompt.text = word + " "
        prompt.move_cursor((0, len(word) + 1))

    # ── 输入历史（↑↓） ──────────────────────────────────────

    def _move_history(self, delta: int) -> None:
        if not self._hist:
            return
        self._hist_idx = max(0, min(len(self._hist), self._hist_idx + delta))
        prompt = self.query_one("#prompt", PromptInput)
        value = "" if self._hist_idx >= len(self._hist) else self._hist[self._hist_idx]
        prompt.text = value
        if value:
            prompt.move_cursor((value.count("\n"), len(value.rsplit("\n", 1)[-1])))

    def action_hist_prev(self) -> None:
        self._move_history(-1)

    def action_hist_next(self) -> None:
        self._move_history(1)

    # ── 动作 ─────────────────────────────────────────────────

    def action_interrupt(self) -> None:
        if self._is_busy() and portal.is_open:
            portal.run.cancel()
            self._line("⏹ 已请求打断", "yellow")
            self._refresh_status()
        else:
            self.exit()

    def action_app_quit(self) -> None:
        self.exit()

    def action_stats(self) -> None:
        self.push_screen(StatsModal(build_stats_body()))


# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════


def tui_available() -> tuple[bool, str]:
    """L0 判定：TUI 是否可用。返回 (可用, 原因)——降级理由可读，便于排障。"""
    import os
    import sys

    if os.environ.get("FP_NO_TUI"):
        return False, "FP_NO_TUI 已设置"
    if os.environ.get("FP_IS_SUBAGENT") == "1":
        return False, "subagent 子进程"
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False, "非 TTY（管道/重定向）"
    if os.environ.get("TERM", "") == "dumb":
        return False, "TERM=dumb"
    import importlib.util

    if importlib.util.find_spec("textual") is None:
        return False, "textual 未安装"
    return True, ""


async def run_tui(
    io: TUIIO,
    *,
    directive: ReloadDirective | None = None,
    resume: str | None = None,
) -> None:
    """运行 TUI 直至退出（main() 的 finally 负责 ctl.close）。"""
    app = FPApp(io, directive=directive, resume=resume)
    io.attach(app)
    await app.run_async()


__all__ = [
    "FPApp",
    "PromptInput",
    "TUIIO",
    "TUILogger",
    "route_input",
    "run_tui",
    "tui_available",
]
