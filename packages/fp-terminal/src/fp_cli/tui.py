"""TUI — Textual 交互界面（L0 主路径）

降级链（``main._tui_available`` 决策）：
    L0 TUI（本模块） → L1 经典 print REPL（裸 input，无补全） → L2 headless/-m/管道

渲染模型：VerticalScroll 消息流 + 常驻输入区（多行 PromptInput + 发送按钮，状态条/补全浮层）。
    - 已固化内容 = 追加的 Static/Markdown 子件（顺序天然正确，历史即日志）
    - 流式中内容 = 单个 live 子件，**时间节流**（``STREAM_FLUSH_INTERVAL``，
      约 20fps 上限）update，段末固化（live 引用置空，子件保留可见以迎接下一段）

渲染成本模型（改这里之前先看）：
    任何一次内容变更 = 一整帧。Textual 的自动高度子件一旦变高，就触发
    ``_arrange_root(visible_only=False)`` 全量重排——**遍历 #chat 的全部子件**，
    成本随历史块数线性增长；随后的重绘按可见区算。因此「多少帧」比「多少 token」
    重要得多：一 token 一帧会把事件循环占满（实测每帧 10–30ms），
    键盘/滚轮事件只能排队 → 卡顿。同理，内容没变就不要碰 widget：
    ``Static.update`` 默认 ``layout=True``，空转一次就是一次全屏重排。

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
import time
from collections import Counter
from typing import TYPE_CHECKING, Any, cast

from rich.console import Console
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.geometry import Offset
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.theme import BUILTIN_THEMES
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Button, Collapsible, Input, Markdown, OptionList, Static, TextArea

from fp_core.api import IOChannel, portal
from fp_core.logger import Logger

from . import charts, completion
from . import tui_ctxview as ctxview
from .appearance import (
    UNSET,
    AppearancePanel,
    install_appearance,
    install_theme,
    persist_tui,
    tui_glyph,
    tui_style,
)
from .history import append_history, load_history
from .keys_panel import KeyCapture, KeysPanel
from .tui_actions import (
    ACTIONS,
    BY_ID,
    UNBOUND_KEY,
    by_scope,
    canonical_key,
    check_clashes,
    display_keys,
    effective_key,
    load_keymap,
)

if TYPE_CHECKING:
    from textual.theme import Theme

    from fp_core.api.types import ReloadDirective


#: 动作 id → App 上的方法名（少数动作两者不同）。
_ACTION_METHODS: dict[str, str] = {
    "keys": "open_keys",
    "appearance": "open_appearance",
}


#: 输入框聚焦时**必须**截获的键 → App 方法名。
#:
#: 为什么要截获：TextArea 声明消费一切可打印字符，被 focused widget 消费的键会从
#: 上层绑定表里**就地删除**（``priority=True`` 也救不回来，因为它的 Binding 已经
#: 不在表里了）。本方法早于所有 widget 绑定，行为确定。
#:
#: 为什么是**函数**而不是常量：用户可以在面板里改键，覆盖后的键同样要被截获，
#: 否则「面板里改了键、输入框里按却没反应」。故必须在 keymap 生效后重建。
def build_key_actions(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """按生效键位构建截获表（只含修饰键组合；裸键语义归焦点 widget）。"""
    table: dict[str, str] = {}
    for action in ACTIONS:
        if action.scope not in ("global", "chat"):
            continue
        key = display_keys(action, overrides).strip()
        if key and "+" in key:
            table[key] = _ACTION_METHODS.get(action.id, action.id)
    return table


#: 输入框内编辑动作 → TextArea 的实际 action 方法名（绝大多数同名）。
_PROMPT_EDIT_METHODS: dict[str, str] = {
    "delete_to_end_of_line": "delete_to_end_of_line_or_delete_line",
}


def _kb_edit(action_id: str) -> Binding:
    """生成输入框编辑绑定：键位来自动作目录，动作为 TextArea 原生方法。

    带 ``id`` 才能被 ``App.set_keymap`` 覆盖，也才能在面板里改键。
    """
    spec = BY_ID[action_id]
    return Binding(
        spec.key or UNBOUND_KEY,
        _PROMPT_EDIT_METHODS.get(action_id, action_id),
        spec.label,
        show=False,
        priority=True,
        id=action_id,
    )


def _kb(action_id: str, method: str, *, priority: bool = False, show: bool = True) -> Binding:
    """由动作目录（``tui_actions.ACTIONS``）生成 ``Binding``。

    默认键串**只在目录里写一次**；本函数把「动作 → action 方法」的映射
    与优先级/可见性钉在 widget 侧，避免两处各存一份键位。

    Args:
        action_id: 动作目录中的 id，同时成为 ``Binding.id``（keymap 覆盖键）。
        method: ``Binding.action``——app/screen/widget 上的方法名（不含前缀）。
        priority: 是否优先于焦点 widget 的内建绑定（消息区动作一律 True）。
        show: 是否进 Textual 自带的 footer/命令面板。
    """
    spec = BY_ID[action_id]
    return Binding(
        # 目录里 key 为空 = **未绑定**（留给用户自设）：Binding 不接受空键，
        # 用按不到的哨兵占位，用户设了键即被 keymap 覆盖。
        spec.key or UNBOUND_KEY,
        method,
        spec.label,
        show=show,
        priority=priority,
        id=spec.id,
    )


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


#: 流式合批节流：两次冲刷的最小间隔（秒）→ 屏幕刷新上限约 20fps。
#: 一次冲刷 = 一整帧（重排 + 重绘可见区 + 终端写入）：SSE token 逐块冲刷
#: 会让事件循环饱和（实测每 token 约 2ms CPU），且帧内并不产生可见差异。
STREAM_FLUSH_INTERVAL = 0.05

#: 补全浮层：候选窗一屏可见行数（超出部分随选择滚动窗口，选中项恒可见）。
HINT_ROWS = 6
HINT_ROWS_NARROW = 3
#: 候选窗最大列宽 / 帮助栏最小可用列宽。两栏加起来放不下就切上下叠放。
HINT_LIST_MAX_W = 30
HINT_DETAIL_MIN_W = 30
#: 帮助栏最多占几行（含名字/描述/正文/键位提示）——超出在末行前插省略号。
HINT_DETAIL_ROWS = 10
HINT_DETAIL_ROWS_NARROW = 8

#: 仅用于**度量**的 rich 控制台（CJK 记 2 格）。不做实际输出，Terminal 输出归 Textual。
_MEASURE = Console(width=80)


def _wrap_cells(text: Text, width: int, max_rows: int) -> Text:
    """按单元格宽度折行并限行，返回已折好的多行 ``Text``。

    为什么自己折行而不交给 ``text-wrap: wrap``：Textual 的 ``height: auto``
    在**水平容器**里按「未折行」的行数计算高度（实测 9 逻辑行折成 11 行后，
    容器只给了 5 行高，后半被静默裁掉）。先把折行结果算好交过去，宽度与高度
    就对得上——这是「多行信息不折叠」的前提，别改回自动折行。

    Args:
        text: 待折行的富文本（span 样式随行保留）。
        width: 目标列宽（单元格数，CJK 记 2）。
        max_rows: 最多保留的行数。超了保留首部 + 尾行（键位提示），中间插省略号。
    """
    lines = text.wrap(_MEASURE, max(1, width))
    if len(lines) > max_rows > 1:
        keep = lines[: max_rows - 2]
        lines = [*keep, Text("…", style=tui_style("keys")), lines[-1]]
    out = Text()
    for i, line in enumerate(lines):
        if i:
            out.append("\n")
        out.append_text(line)
    return out


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
        # 上次冲刷时刻（monotonic）：下次冲刷至少间隔 STREAM_FLUSH_INTERVAL
        self._last_flush = 0.0
        # 在途的节流定时器（立即冲刷时取消，防重复冲刷）
        self._flush_timer: Timer | None = None
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
        self._last_flush = time.monotonic()
        # 立刻冲刷时取消在途的节流定时器：否则它会到点再空刷一次（多一帧）
        timer, self._flush_timer = self._flush_timer, None
        if timer is not None:
            timer.stop()
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
        """安排一次合批冲刷：最早下一 tick，且距上次冲刷不短于节流间隔。

        ``call_later`` 只是在下一 tick 插一条回调——**本身不做时间节流**，
        SSE 每块一次 await，于是「一 token 一帧」。这里改用时间门：间隔未满
        就交给 ``set_timer`` 到点再刷（同一时刻至多一个待触发，见
        ``_flush_scheduled``），token 攒成一批只画一帧。
        """
        if self._flush_scheduled or not self._ready:
            return
        assert self._app is not None
        self._flush_scheduled = True
        delay = STREAM_FLUSH_INTERVAL - (time.monotonic() - self._last_flush)
        if delay <= 0:
            self._app.call_later(self._timed_flush)
        else:
            self._flush_timer = self._app.set_timer(delay, self._timed_flush)

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
            self._emit("status", f"{tui_glyph('think')} 思考中…")

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
        timer, self._flush_timer = self._flush_timer, None
        if timer is not None:
            timer.stop()

    def tool_call(self, name: str, args: dict[str, Any]) -> None:
        self._flush_now()  # 先落盘在途流段，再插工具卡（顺序不变量）
        # 结构化载荷：完整参数交给展示层（卡体），摘要由展示层压缩
        self._emit("tool", {"name": name, "args": args})

    def tool_result(self, result: str) -> None:
        self._flush_now()
        self._emit("tool_result", {"text": result})

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

    BINDINGS = [_kb("modal_close", "cancel", show=False)]

    def __init__(self, prompt: str, options: list[str] | None, suggest: str) -> None:
        super().__init__()
        self._prompt = prompt
        self._options = options or []
        self._suggest = suggest

    def compose(self) -> ComposeResult:
        with Container(id="ask_box"):
            yield Static(f"{tui_glyph('ask')} {self._prompt}", id="ask_prompt")
            if self._options:
                yield OptionList(*self._options, id="ask_opts")
            hint = "Enter 提交 · Esc 跳过"
            if self._suggest:
                hint = f"{tui_glyph('hint')} 推荐默认值：{self._suggest}（空回车采纳） · {hint}"
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
        _kb("modal_close", "close", show=False),
        _kb("modal_confirm", "close", show=False),
    ]

    def __init__(self, body: Text) -> None:
        super().__init__()
        self._body = body

    def compose(self) -> ComposeResult:
        with Container(id="stats_box"):
            yield Static(f"{tui_glyph('stats')} 会话统计", id="stats_title")
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
    text.append(charts.spark_segments(lens) or "（数据不足）", style=tui_style("spark"))
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


#: 输入框占位文案。**刻意不提按键**：换行/发送都是可改键的动作
#: （``insert_newline``/``submit``），写死「Enter 换行 · Shift+Enter 发送」
#: 在用户改键之后就变成谎话——要查键位请按「键位设置」或 ``fp --keys``。
PROMPT_HINT_IDLE = "消息…（/ 命令）"
PROMPT_HINT_BUSY = "运行中 · 输入将排队注入（斜杠禁用）"


class PromptInput(TextArea):
    """多行输入框：换行键默认 ``Enter``（``insert_newline`` 动作，可改键），Shift+Enter 发送。

    ↑↓ 语义按光标位置分流（确定性，不依赖按键冒泡）：
    - 补全浮层打开时（正文以 '/' 开头且有候选）→ ↑↓ 只在候选间挪选择，
      不碰正文、也不翻历史；Esc 关浮层（再输入自动重开），Tab 插入选中项；
    - 否则：光标未到边界（↑ 非首行 / ↓ 非末行）→ 光标移动（多行编辑）；
    - ↑ 到首行再按 → 上一条历史，光标落在其**最后一行**（接着 ↑ 可继续上溯）；
    - ↓ 到末行再按 → 下一条历史，光标落在其**第一行**（接着 ↓ 可继续前行）。
    空正文即单行，↑↓ 直接分流到历史，沿袭单行 Input 时代的行为。
    """

    BINDINGS = [
        _kb("history_prev", "prompt_up", show=False),
        _kb("history_next", "prompt_down", show=False),
        _kb("hint_dismiss", "dismiss_hint", show=False),
        # 输入框内编辑动作：原硬编码在 TextArea.BINDINGS（用户既看不见也改不了，
        # 还会与上层动作静默互抢）。登记进动作目录后：面板可见 / 可改键 / 冲突可检。
        *[_kb_edit(_a.id) for _a in by_scope("prompt_edit")],
    ]

    def action_prompt_up(self) -> None:
        app = cast("FPApp", self.app)  # pyright: ignore[reportUnknownMemberType]
        if self.cursor_location[0] > 0:
            self.action_cursor_up()
        elif not app.hint_move(-1):
            app.action_hist_prev()

    def action_prompt_down(self) -> None:
        app = cast("FPApp", self.app)  # pyright: ignore[reportUnknownMemberType]
        if self.cursor_location[0] < self.document.line_count - 1:
            self.action_cursor_down()
        elif not app.hint_move(1):
            app.action_hist_next()

    def action_dismiss_hint(self) -> None:
        """Esc：只关浮层，不清正文（正文不匹配任何命令时浮层本来也没开）。"""
        cast("FPApp", self.app).dismiss_hint()  # pyright: ignore[reportUnknownMemberType]

    def action_insert_newline(self) -> None:
        """插入换行——TextArea 原生 ``enter`` 行为的可改键版本。"""
        start, end = self.selection
        if start == end:
            self.insert("\n")
        else:
            self.replace("\n", start, end, maintain_selection_offset=False)

    async def _on_key(self, event: events.Key) -> None:
        """让「哪个键换行」由动作目录决定，而不是 TextArea 的硬编码。

        ``_kb_edit`` 生成的 binding 已能被 keymap 覆盖，但默认的裸键 ``enter`` 与
        TextArea ``_on_key`` 里硬编码的 ``insert_values = {"enter": "\\n"}`` 同源：
        用户把「换行」改到别的键之后，Enter 仍会插入换行。故在此兜底——只有
        **生效换行键**才换行（同字节别名按规范键比较），被改走的 Enter 直接吞掉，
        其余键照旧交给 TextArea。
        """
        app = cast("FPApp", self.app)  # pyright: ignore[reportUnknownMemberType]
        newline = canonical_key(effective_key(app, "insert_newline"))
        if newline and canonical_key(event.key) == newline:
            event.stop()
            event.prevent_default()
            self.action_insert_newline()
            return
        if canonical_key(event.key) == "enter":
            # 换行键被改走或显式取消绑定：Enter 不再换行
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)


# ══════════════════════════════════════════════════════════════
# 工具卡片 — 调用/返回的可折叠展示（TUI 版 webui tool-msg）
# ══════════════════════════════════════════════════════════════

#: 工具结果卡展开时的最大渲染行数：超出即截断，防超长输出拖垮终端布局。
#: 折叠态只露标题（无内容渲染开销），故默认成本不高于旧的「6 行预览」。
TOOL_RESULT_MAX_LINES = 200


class ToolCard(Collapsible):
    """工具调用/返回的可折叠卡片。

    与 webui 的 tool-msg 同构：默认折叠，点击标题栏（或聚焦后回车）开合。
    标题用 rich ``Text`` 承载——工具名/参数里的 ``[`` 原样显示，不会被当作
    Textual 标记解析。完整内容放卡体，溢出由构造方截断。
    """

    def __init__(self, header: Text, body: str, *, card_class: str) -> None:
        super().__init__(
            Static(Text(body)),
            title=cast("str", header),  # rich Text：from_text 保形且不解析标记
            collapsed=True,
            classes=card_class,
        )


def _tool_card(payload: Any) -> ToolCard:
    """构造工具调用卡：标题=工具名，展开=完整参数（pretty JSON）。"""
    data: dict[str, Any] = cast("dict[str, Any]", payload) if isinstance(payload, dict) else {}
    name = str(data.get("name", ""))
    args = data.get("args", {})
    try:
        pretty = json.dumps(args, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        pretty = str(args)
    header = Text(f"{tui_glyph('tool')} ", style=tui_style("tool")) + Text(name, style=tui_style("tool"))
    return ToolCard(header, pretty, card_class="tool-card")


def _tool_result_card(payload: Any) -> ToolCard:
    """构造工具返回卡：标题=返回行数，展开=完整结果（超长截断）。"""
    data: dict[str, Any] = cast("dict[str, Any]", payload) if isinstance(payload, dict) else {}
    text = str(data.get("text", ""))
    lines = text.splitlines() or [""]
    total = len(lines)
    body = text
    if total > TOOL_RESULT_MAX_LINES:
        body = "\n".join(lines[:TOOL_RESULT_MAX_LINES]) + f"\n… （共 {total} 行，已截断）"
    header = Text(f"{tui_glyph('tool_result')} ", style=tui_style("tool_result")) + Text(
        f"返回 · {total} 行", style=tui_style("tool_result")
    )
    return ToolCard(header, body, card_class="tool-result-card")


class ThinkCard(Collapsible):
    """思考流的可折叠卡片 — 与 webui think-msg 同构，与 ToolCard 开合样式统一。

    自动过程由 FPApp 驱动（两状态机 ``_think_card``/``_think_active``）：
    - 思考段开始 → 开卡，默认**展开**，body 跟随 token 增量渲染；
    - **首个正文 token 或工具调用到达 → ``fold()``** 折叠为「思考 · N 字」单行摘要；
    - 点击标题栏（或聚焦后回车）随时开合，全文保留在卡体；
    - ``finalize`` 只断流不折叠：纯思考轮的卡保持展开收尾（若随后 tool 到达
      仍会被折叠）；开新卡前旧卡必先折叠——任一时刻至多一张展开的思考卡。

    标题不用符号字形（💭 气泡已弃用），纯文本 + 字数统计；字数在折叠时定格，
    折叠后的迟到 token 仍进 body（展开可见），不再改摘要行。
    """

    def __init__(self) -> None:
        self._text = ""
        self._body = Static(Text(""))
        super().__init__(
            self._body,
            title=cast("str", self._header(0)),
            collapsed=False,  # 流式期展开；首个正文 token 折叠
            classes="think-card",
        )

    @staticmethod
    def _header(chars: int) -> Text:
        return Text("思考", style=tui_style("think")) + Text(f" · {chars} 字", style=tui_style("think"))

    @property
    def text(self) -> str:
        return self._text

    def feed(self, token: str) -> None:
        """累积思考 token：body 全量更新；未折叠时标题字数实时跟随。"""
        if not token:
            return
        self._text += token
        self._body.update(Text(self._text, style=tui_style("think")))
        if not self.collapsed:
            self.title = self._header(len(self._text))

    def fold(self) -> None:
        """折叠为摘要行（幂等）：标题定格最终字数，全文保留在卡体可再展开。"""
        if self.collapsed:
            return
        self.title = self._header(len(self._text))
        self.collapsed = True


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
    .tool-card, .tool-result-card, .think-card {
        border: none; padding: 0; background: transparent; height: auto;
    }
    .tool-card > CollapsibleTitle,
    .tool-result-card > CollapsibleTitle,
    .think-card > CollapsibleTitle { padding: 0 1; }
    .tool-card > Contents, .tool-result-card > Contents, .think-card > Contents { padding: 1 0 1 2; }
    /* 斜杠补全浮层：左候选窗（选中行高亮）+ 右帮助栏（自动换行）。
       窄屏（< HINT_NARROW_WIDTH）切成上下叠放，否则两栏都只剩碎片。 */
    #hint {
        height: auto; max-height: 11;
        background: $panel; color: $text;
        padding: 0 1; display: none;
    }
    #hint.visible { display: block; }
    #hint_list {
        width: auto; max-width: 30;
        height: auto;
        text-wrap: nowrap;   /* 候选一律单行：窗口行数才能和 HINT_ROWS 对上 */
    }
    #hint_detail {
        width: 1fr; height: auto;
        padding: 0 0 0 2;
        border-left: solid $surface;
    }
    #hint.narrow { layout: vertical; max-height: 13; }
    #hint.narrow #hint_list { width: 100%; max-width: 100%; }
    #hint.narrow #hint_detail {
        width: 100%; padding: 0;
        border-left: none; border-top: solid $surface;
    }
    #prompt_row {
        dock: bottom;
        height: auto;
        padding: 0;
        background: $background;
        border-top: tall $surface;
        border-bottom: tall $surface;
    }
    #prompt {
        width: 1fr;
        height: auto;
        max-height: 6;
        overflow-y: auto;
        border: none;
        padding: 0 1;
        background: $background;
        color: $text;
    }
    #send_btn {
        width: 3;
        min-width: 3;
        height: 100%;
        border: none;
        padding: 0;
        background: $background;
        color: $text;
        content-align: center middle;
    }
    #send_btn:hover { color: $accent; text-style: bold; }
    #send_btn:focus { color: $accent; background: $accent 15%; }
    /* 键盘可达的前提是焦点可见：卡片标题是 Ctrl+N/P 与 Tab 的落点，
       默认没有任何聚焦反馈，不补高亮则键盘导航形同虚设。 */
    CollapsibleTitle:focus { background: $accent 25%; text-style: bold; }
    CollapsibleTitle:hover { text-style: bold; }
    #ask_box, #help_box, #stats_box {
        width: 80%; max-width: 100; height: auto;
        max-height: 90%;
        border: round $accent; background: $panel;
        padding: 1 2; margin: 2 1;
    }
    #ask_prompt, #help_title, #stats_title { text-style: bold; color: $accent; }
    #ask_opts { max-height: 14; }
    #ask_hint, #help_hint, #stats_hint { color: $text-muted; }
    #help_scroll { height: auto; max-height: 60vh; }
    #help_body { width: auto; height: auto; }
    AskModal, StatsModal { align: center middle; background: $background 60%; }
    """

    #: 待抑制的「静默主题变更」计数：apply_theme_silently 每真正改一次主题 +1，
    #: 每收到一次 theme_changed_signal −1。用计数而不是布尔量，是因为信号投递
    #: 不是同步的——`_watch_theme` 先 `call_next` 投递 publish，publish 再对每个
    #: 订阅者 `call_next`（textual/signal.py::Signal.subscribe）→ 两跳之后才轮到
    #: `_on_theme_changed`。任何「置 True → 关闭面板时置回 False」的写法都会在
    #: 第二跳之前把抑制解掉（实测：Esc 一按，config 里凭空多出 "theme"）。计数
    #: 与信号一一对应，跟中间隔了几跳无关。
    _silent_theme_changes = 0

    # 键位一律取自 tui_actions.ACTIONS（id 即 keymap 覆盖键）；改默认键只改那张表。
    # ctrl+shift+c 与 ctrl+c 同义：kitty 协议终端下两者字节不同（99;6u）。
    # ctrl+j 是发送兜底：不支持 kitty 协议的终端无法区分 Shift+Enter（退化为 CR），
    # 而 LF(0x0A) 是所有终端都可区分的字节。
    # 消息区动作全部 priority：输入框最多 6 行，翻页/卡片导航在其中的价值远低于
    # 滚消息；让位换来「焦点在哪都能操作消息区」的确定性——不用先把焦点 Tab 出去。
    BINDINGS = [
        _kb("submit", "submit_prompt", priority=True, show=False),
        _kb("copy_selection", "copy_selection", priority=True),
        _kb("interrupt", "interrupt", priority=True),
        _kb("quit", "app_quit", priority=True),
        _kb("stats", "stats"),
        _kb("appearance", "open_appearance", priority=True),
        # ctrl+k：键位设置（捕获式改键）；键位配坏时仍可输入 /keys 进入
        _kb("keys", "open_keys", priority=True),
        _kb("complete", "tab_complete", priority=True, show=False),
        _kb("scroll_page_up", "scroll_page_up", priority=True, show=False),
        _kb("scroll_page_down", "scroll_page_down", priority=True, show=False),
        _kb("jump_latest", "jump_latest", priority=True, show=False),
        _kb("card_next", "card_next", priority=True, show=False),
        _kb("card_prev", "card_prev", priority=True, show=False),
        _kb("card_toggle", "card_toggle", priority=True, show=False),
        _kb("cards_toggle_all", "cards_toggle_all", priority=True, show=False),
    ]

    def __init__(
        self,
        io: TUIIO,
        *,
        directive: ReloadDirective | None = None,
        resume: str | None = None,
        open_keys: bool = False,
    ) -> None:
        super().__init__()
        # fp --keys：启动即开键位面板（不依赖任何键位，按键失灵时的兜底入口）
        self._open_keys = open_keys
        # 输入框聚焦时截获的键（on_mount 按生效 keymap 重建）
        self._key_actions: dict[str, str] = build_key_actions()
        self._io = io
        self._directive = directive
        self._resume = resume
        # 挂载状态：决定 IO 输出走消息还是缓冲
        self.mounted = False
        # 本地发送标志（handler 同步置位，防连按双发）
        self._sending = False
        # 流式 live 子件引用（None = 当前无在途正文流段）
        self._live: Markdown | None = None
        self._live_buf = ""
        # 当前思考卡（跨轮保留引用，供正文/工具调用到达时折叠）+ 是否正在接收
        # 本轮思考 token（finalize 只断流不丢卡：纯思考轮的卡留给 tool 收尾折叠）
        self._think_card: ThinkCard | None = None
        self._think_active = False
        # 状态附注（spinner 文本）
        self._status_note = ""
        # 状态条/占位符的最近一次渲染结果：内容没变就不重画（见 _refresh_status）
        self._status_text: Text | None = None
        self._placeholder = ""
        # 输入历史
        self._hist: list[str] = []
        self._hist_idx = 0
        # 斜杠补全浮层：当前候选全表 + 选中下标（↑↓ 移动，Tab 插入选中项）
        self._hint_matches: list[completion.Candidate] = []
        self._hint_idx = 0
        # ── 上下文对账（视图 == transcript 非 system 投影，见 _sync_view）──
        # _ctx_settled：上次确认「视图与之相符」的投影指纹；_ctx_probe：O(1) 快查
        # 基线（条数 + 末条角色/长度），用于把空闲期的对账成本压到接近零。
        self._ctx_settled: tuple[str, ...] = ()
        self._ctx_probe: tuple[int, str, str] | None = None
        self._ctx_msgs = 0
        # 流式/忙碌期检测到改写 → 记账，收尾拍统一补对账
        self._sync_pending = False

    def get_default_screen(self) -> Screen[None]:
        """默认屏换用带 #6643 竞态修复的 FPDefaultScreen。"""
        return FPDefaultScreen(id="_default")

    # ── 组装 ─────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with VerticalScroll(id="chat"):
            yield Static("", id="chat_anchor")
        # 补全浮层：左候选窗 + 右帮助栏（窄屏由 .narrow 切成上下叠放）
        with Horizontal(id="hint"):
            yield Static(id="hint_list")
            yield Static(id="hint_detail")
        with Horizontal(id="prompt_row"):
            yield PromptInput(
                placeholder=PROMPT_HINT_IDLE,
                id="prompt",
            )
            yield Button(tui_glyph("send"), id="send_btn")

    async def on_mount(self) -> None:
        self.mounted = True
        self._hist = load_history(_history_path())
        self._hist_idx = len(self._hist)
        # 主题切换落盘：Ctrl+P 命令面板 → Theme 选完即写回 config.json tui.theme
        self.theme_changed_signal.subscribe(self, self._on_theme_changed)
        # 挂载前缓冲的 agent 输出（含泵起轮残段）→ FIFO 冲刷
        for msg in self._io.take_pending():
            self._handle_io(msg)
        # 欢迎 + reload 续接
        self._render_welcome()
        if self._directive is not None and self._directive.should_continue:
            self.run_worker(self._continue_round())
        elif self._directive is not None and self._directive.notice:
            self._line(self._directive.notice, tui_style("notice"))
        # 状态刷新（覆盖泵起轮的忙碌态——它们不经本应用）
        # 键位覆盖（config.json tui.keys）必须在首个按键到达前生效
        keymap, problems, stale = load_keymap()
        if stale:
            # 陈旧条目（动作已改名或删除）是我们自己留下的残渣，静默自愈：
            # 删掉再继续，不给用户看一条他无从理解的提示。
            persist_tui({f"tui.keys.{action_id}": UNSET for action_id in stale})
        # 与默认键的冲突 Textual 自己不报（只在 keymap 内部自相冲突时回调），自行校验
        self._clash_notices = check_clashes(keymap)
        problems += self._clash_notices
        if keymap:
            self.set_keymap(keymap)
        # 截获表按**生效**键位重建：用户覆盖的键同样要在输入框聚焦时被截获，
        # 否则「面板里改了键、输入框里按却没反应」。
        self._key_actions = build_key_actions(keymap)
        self.set_interval(0.4, self._refresh_status)
        # 立对账基线：启动时的 transcript（-r 续会话）**不**触发重画——此刻视图
        # 还是欢迎屏，重画历史既慢又突兀。之后任何前缀断裂都会走到 _sync_view。
        self._mark_settled()
        self._refresh_status()
        # 坏配置合并成一条明示（逐条刷屏同样是噪音）
        if problems:
            detail = problems[0] if len(problems) == 1 else f"{len(problems)} 项 —— " + "；".join(problems)
            self._line(f"键位配置：{detail}", tui_style("warning"))
        self.query_one("#prompt", PromptInput).focus()
        if self._open_keys:
            self.action_open_keys()

    def action_open_appearance(self) -> None:
        """Ctrl+T：外观设置面板的**开关**（主题/配色/样式/符号/开关）。

        与 ``Ctrl+K`` 同一套语义：再按一次关闭，避免重复按键叠层。
        """
        if isinstance(self.screen, AppearancePanel):
            self.pop_screen()
            return
        self.push_screen(AppearancePanel())

    def action_open_keys(self) -> None:
        """Ctrl+K：键位设置面板的**开关**（捕获式改键）。

        再按一次关闭 —— 开关式而非只开不关，避免重复按键把同一个面板叠成多层。
        """
        if isinstance(self.screen, KeysPanel):
            self.pop_screen()
            return
        self.push_screen(KeysPanel())

    def refresh_key_actions(self, keymap: dict[str, str] | None = None) -> None:
        """按新 keymap 重建输入框截获表。

        面板改键后必须调用：截获表若停留在挂载时算出的旧值，用户新设的键
        在输入框里按下去不会有任何反应（``set_keymap`` 只改 widget 绑定链，
        不改这张表）。
        """
        self._key_actions = build_key_actions(keymap)
        self.refresh_bindings()

    async def _check_bindings(self, key: str, priority: bool = False) -> bool:
        """键位面板（含其捕获子屏）打开期间让出**全部**可配置绑定。

        ``priority=True`` 的绑定走完整绑定链（穿透 modal），若不让出，用户在
        捕获界面按 ``Ctrl+Q`` 会直接退出程序、按 ``Ctrl+J`` 会发消息。覆盖的是
        textual 私有钩子（无公开等价物），行为由 ``test_ctrl_q_inside_capture_does_not_quit``
        锁定 —— 上游若改语义该测试会红。

        **面板本身也要让，不只是捕获子屏**：面板的「选中动作」就是按 ``Enter``。
        用户一旦把「发送消息」改到 ``Enter``，按 ``Enter`` 会先被 App 的 priority
        绑定吃掉（发出消息而不是进入捕获态），键位从此改不动——自锁。面板打开
        期间只留面板自己的 ``BINDINGS``、widget 原生绑定与 Tab 焦点循环，等价于
        「所有快捷键被取消」；关闭即恢复（绑定本身没被销毁，见 ``KeysPanel.on_unmount``）。
        """
        if isinstance(self.screen, KeyCapture):
            return False
        # Tab 的语义分两种：在主屏输入框里是「补全提示」，其它任何地方（外观 /
        # 键位 / 统计等 modal，或主屏焦点不在输入框）都是「焦点移到下一个控件」。
        # ``tab_complete`` 是 priority 绑定（App 级），不门控的话 modal 里的 Tab
        # 一律变成空操作 —— 面板内的焦点循环就断了。
        if key == "tab" and not (isinstance(self.screen, FPDefaultScreen) and isinstance(self.focused, PromptInput)):
            self.screen.focus_next()
            return True
        if isinstance(self.screen, KeysPanel):
            # 面板打开期间 = **App 级动作全部取消**，只有两处例外：
            # ① 面板自身的开关（``keys``，且必须是带修饰键的组合）——否则「Ctrl+K
            #    打开的面板 Ctrl+K 关不掉」。裸键一律不放行：Enter 选中、Tab 换位、
            #    Esc 关闭这些面板交互归 widget/screen 原生绑定；
            # ② screen/widget 级绑定照常——「选中动作」是 OptionList 的 non-priority
            #    绑定（``enter → select``），若整层短路，按 Enter 什么都不发生。
            toggle = canonical_key(effective_key(self, "keys"))
            if priority and "+" in toggle and canonical_key(key) == toggle:
                return await self.run_action("open_keys", self)
            return await self._bindings_without_app(key, priority)
        # 「发送消息」在非主屏 / 焦点不在输入框时不作数：它绑的是 Enter / Shift+Enter
        # 这类**交互键**，modal 里真正需要它的是「选中列表项」「确认提问」。App 级
        # priority 绑定会穿透弹窗抢先吃掉，于是用户的 Enter 变成「发出一句话」。
        if (
            priority
            and key == canonical_key(effective_key(self, "submit"))
            and not (isinstance(self.screen, FPDefaultScreen) and isinstance(self.focused, PromptInput))
        ):
            return await self._bindings_without_app(key, priority)
        # 输入框聚焦时，TextArea 会把一部分 ctrl 组合当自己的编辑键抢走（它声明
        # 消费一切可打印字符），priority 绑定也救不回来。这类键在这里截获——
        # 本方法早于所有 widget 绑定，行为确定。
        if not priority and (action_id := self._key_actions.get(key)) is not None:
            return await self.run_action(action_id, self)
        return await super()._check_bindings(key, priority)

    async def _bindings_without_app(self, key: str, priority: bool) -> bool:
        """走绑定链但**跳过 App 自身那一层**（screen / widget 的绑定照常生效）。

        直接 ``return False`` 会短路整条链——连 ``OptionList`` 的 ``enter → select``
        也一起没了，按 Enter 什么都不发生。故逐层复刻 textual 的遍历，只跳过
        ``namespace is self``。覆盖的是私有属性（同 ``_check_bindings``），行为由
        ``test_panel_suspends_other_shortcuts_and_restores_on_close`` 锁定。
        """
        chain = (
            reversed(self.screen._binding_chain)  # pyright: ignore[reportPrivateUsage]
            if priority
            else self.screen._modal_binding_chain  # pyright: ignore[reportPrivateUsage]
        )
        for namespace, bindings in chain:
            if namespace is self:
                continue
            for binding in bindings.key_to_bindings.get(key, ()):
                if binding.priority == priority and await self.run_action(binding.action, namespace):
                    return True
        return False

    def apply_theme_silently(self, name: str) -> None:
        """改主题但**不落盘**——外观面板的实时预览 / Esc 回滚专用。

        计数与信号一一对应：只有「值真的变了」才 +1（reactive 的 watcher 只在值
        变化时触发，值没变就没有信号，加计数会错位吞掉下一次正常落盘）。
        """
        if self.theme == name:
            return
        self._silent_theme_changes += 1
        self.theme = name

    def _on_theme_changed(self, theme: Theme) -> None:
        """主题变更 → 写回 config.json（``tui.theme``），跨重启存活。

        两条路径会触发本 watcher：
        - ``Ctrl+P`` → 命令面板 Theme（Textual 自带，仍在）
        - 外观面板的实时预览（Ctrl+T）——**它自己负责落盘**，故走
          ``apply_theme_silently``：本次信号消费掉一个抑制计数后直接返回。

        只落盘**内置**主题名（``BUILTIN_THEMES``）：
        - ``fp-custom``（palette 注册出来的）不算——它是对 config 里 palette 的
          派生结果，写进去反而变成「config 说主题叫 fp-custom，但启动时又不认识」
          的鬼状态。palette 本来就写在 config 里，重启后照样生效。
        - 注意不能用 ``available_themes`` 判：``register_theme`` 之后它也会包含
          ``fp-custom``，判了等于没判。
        - 落盘失败静默（persist_tui 内部兜住），不打断交互。
        """
        if self._silent_theme_changes > 0:
            self._silent_theme_changes -= 1
            return
        if theme.name not in BUILTIN_THEMES:
            return
        if persist_tui({"tui.theme": theme.name}):
            self._line(
                f"{tui_glyph('hint')} 主题 → {theme.name}（已记入 config.json）",
                tui_style("hint"),
            )

    async def _continue_round(self) -> None:
        if self._directive is not None and self._directive.notice:
            self._line(self._directive.notice, tui_style("notice"))
        self._line(f"{tui_glyph('reload')} reload 续接：已恢复会话，继续上一轮对话…", tui_style("notice"))
        try:
            await portal.run.continue_()
        except (SystemExit, asyncio.CancelledError):
            raise
        except Exception as e:  # noqa: BLE001 — 续接失败展示而非崩溃
            self._line(f"续接失败: {e}", tui_style("error"))
        finally:
            # 续接同样走通用对账：reload 期间若上下文被换源（handoff/外部改写），
            # 这里与 /sc 走的是同一条路径，无任何特判。
            self._sync_view()

    def _render_welcome(self) -> None:
        try:
            st = portal.run.status
            model, sid = st.model, st.session_id
        except Exception:  # noqa: BLE001
            model, sid = "?", "?"
        head = Text()
        head.append(f"{tui_glyph('bot')} FP · ", style=tui_style("logo"))
        head.append(model, style=tui_style("model"))
        head.append(" · ", style="dim")
        head.append(sid, style="dim")
        self._mount_block(head)
        if self._resume:
            self._line(f"{tui_glyph('hint')} 续会话: {sid}，输入 /help 查看命令", tui_style("hint"))
        else:
            self._line(f"{tui_glyph('hint')} 输入 /help 查看命令，/resume 可回到历史会话", tui_style("hint"))
        # 版本更新提示（安全点之一）：只读 fp 的本地版本缓存，零网络、不砸屏；
        # 后台检查尚未完成（首装/缓存过期）时为 None，晚一个启动周期再提示。
        from .display import take_update_notice

        _notice = take_update_notice()
        if _notice:
            self._line(_notice, tui_style("hint"))
        self._line(f"{tui_glyph('keys')} Ctrl+K 开关键位面板", tui_style("keys"))

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

    def _seal_live(self) -> None:
        """封口在途正文段（子件保留可见，引用置空以迎接下一段）。"""
        self._live = None
        self._live_buf = ""

    def _fold_think(self) -> None:
        """折叠当前思考卡并断开流式接收（正文/工具调用/新思考段到达时，幂等）。"""
        if self._think_card is not None:
            self._think_card.fold()
        self._think_active = False

    def _open_think_card(self) -> None:
        """开一张新思考卡：旧卡先折叠——任一时刻至多一张展开的思考卡。"""
        self._fold_think()
        at_bottom = self._at_bottom()
        self._think_card = ThinkCard()  # 默认展开，跟随流式
        self._think_active = True
        self._mount_block(self._think_card)
        self._follow_scroll(at_bottom)

    def _feed_content(self, chunk: str) -> None:
        """正文流式：单个 Markdown 子件跟随更新（合批见 TUIIO 定时冲刷）。"""
        at_bottom = self._at_bottom()
        if self._live is None:
            self._live_buf = chunk
            md: Markdown = Markdown(chunk)
            self._chat().mount(md)
            self._live = md
        else:
            self._live_buf += chunk
            md = self._live
            assert isinstance(md, Markdown)
            md.update(self._live_buf)
        self._follow_scroll(at_bottom)

    def _feed_think(self, token: str) -> None:
        """思考流式 → ThinkCard（自动展开跟随，正文/工具调用到达即折叠）。

        思考段开始（非流式中）时：先封口在途正文段（交错思考 → 时序正确：
        正文1 → 思考2 → 正文2），再开新卡——上一张卡无论何种状态先折叠。
        """
        if not self._think_active:
            self._seal_live()
            self._open_think_card()
        at_bottom = self._at_bottom()
        card = self._think_card
        assert card is not None  # _open_think_card 刚保证
        card.feed(token)
        self._follow_scroll(at_bottom)

    # ── IO 分发 ──────────────────────────────────────────────

    @on(IOMsg)
    def _on_io(self, msg: IOMsg) -> None:
        self._handle_io(msg)

    def _handle_io(self, msg: IOMsg) -> None:
        kind, payload = msg.kind, msg.payload
        if kind == "info":
            self._line(str(payload), tui_style("info"))
        elif kind == "warning":
            self._line(str(payload), tui_style("warning"))
        elif kind == "error":
            self._line(str(payload), tui_style("error"))
        elif kind == "tool":
            # 工具调用 = 思考段结束信号 → 折叠思考卡；正文段同步封口防串位
            self._fold_think()
            self._seal_live()
            self._mount_block(_tool_card(payload))
        elif kind == "tool_result":
            self._fold_think()
            self._seal_live()
            self._mount_block(_tool_result_card(payload))
        elif kind == "think":
            self._feed_think(str(payload))
        elif kind == "content":
            # 首个正文 token → 思考卡折叠为「思考 · N 字」摘要行（fold 幂等）
            self._fold_think()
            self._feed_content(str(payload))
        elif kind == "finalize":
            # 断开本轮流式接收但**保留卡引用**：纯思考轮的卡保持展开收尾，
            # 若随后还有 tool 到达仍可折叠（tool 是最后一种到达事件）
            self._seal_live()
            self._think_active = False
        elif kind == "status":
            self._status_note = str(payload)
            self._refresh_status()

    # ── 上下文对账 ───────────────────────────────────────────
    #
    # 契约：消息区 == transcript 的非首条 system 投影（见 tui_ctxview 模块说明）。
    # 判据是**状态**而不是事件——不回答「谁改了上下文」，只回答「当前投影是否仍是
    # 已确认投影的尾部追加」。于是 /sc、/back、/resume、shortcircuit 工具、插件、
    # reload 续接、外部客户端改写……全部一视同仁，机制里没有任何名字。

    #: 重画上限：超出只画尾部。长会话（几百条）全量重建会明显卡顿，而用户真正
    #: 要核对的是近处上下文。常量而非特判，调大即可换更多上下文。
    CTX_REDRAW_MAX_UNITS = 200

    def _transcript(self) -> list[dict[str, Any]]:
        """读当前上下文（只读快照）；实例未开启时为空。"""
        if not portal.is_open:
            return []
        try:
            return list(portal.run.transcript)
        except Exception:  # noqa: BLE001 — 对账失败绝不打断主链
            return []

    @staticmethod
    def _probe(messages: list[dict[str, Any]]) -> tuple[int, str, str]:
        """O(1) 快查指纹：条数 + 末条角色 + 末条正文摘要。

        任一项变化即进入全量对账，命中即跳过整轮投影，空闲期成本≈两次字典取值。
        摘要这一项是必需的——只比长度会漏掉「换源但末条等长」（``/resume`` 到
        一个长度相同的会话）这类改写。
        """
        if not messages:
            return (0, "", "")
        # transcript 的元素理论上恒为 dict；外部注入可能塞进异物，故仍做运行时判定
        last = messages[-1] if isinstance(messages[-1], dict) else {}  # pyright: ignore[reportUnnecessaryIsInstance]
        content = last.get("content")
        return (
            len(messages),
            str(last.get("role") or ""),
            ctxview.digest(content) if isinstance(content, str) else "",
        )

    def _mark_settled(
        self,
        units: list[ctxview.Unit] | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """把当前投影记为「已对账」基线（此刻视图与之一致，或本就不打算重画）"""
        messages = self._transcript() if messages is None else messages
        units = ctxview.project(messages) if units is None else units
        self._ctx_settled = ctxview.fingerprint(units)
        self._ctx_probe = self._probe(messages)
        self._ctx_msgs = ctxview.message_count(messages)
        self._sync_pending = False

    def _sync_view(self) -> bool:
        """对账一次：视图需要重建则重建，返回是否重建过。

        时机纪律：流式在途（busy / 正文在写 / 思考卡活跃）时不重画——会把正在
        生成的卡拆掉，且此刻投影还在长。此时只记账，收尾拍统一补。
        """
        if not self.mounted:
            return False
        if self._is_busy() or self._live is not None or self._think_active:
            self._sync_pending = True
            return False
        messages = self._transcript()
        probe = self._probe(messages)
        if not self._sync_pending and probe == self._ctx_probe:
            return False  # 快查未变：一个字节都不动
        units = ctxview.project(messages)
        sig = ctxview.fingerprint(units)
        if ctxview.is_append_only(self._ctx_settled, sig):
            # 纯尾部追加：这些内容事件流已经渲染过，只推进基线，不动视图
            self._ctx_settled, self._ctx_probe = sig, probe
            self._ctx_msgs = ctxview.message_count(messages)
            self._sync_pending = False
            return False
        previous = self._ctx_msgs
        self._mark_settled(units, messages)
        self._redraw_units(
            units,
            note=f"{tui_glyph('reload')} 上下文已同步：视图重建（{previous} → {self._ctx_msgs} 条消息）",
        )
        return True

    def _redraw_units(self, units: list[ctxview.Unit], note: str = "") -> None:
        """清空消息区并按投影重画。

        事件流产生的「非上下文」块（提示行、报错、命令回显）会随之消失——它们
        本就不属于上下文。调用方若需要保留某条回显，必须在重画**之后**补。
        """
        # 在途引用必须先断：否则后续 feed() 会写到已移除的 widget 上
        self._live = None
        self._live_buf = ""
        self._think_card = None
        self._think_active = False

        chat = self._chat()
        # 保留 #chat_anchor（compose 契约的一部分）：整体 remove_children 会让
        # 同 id 的锚点在「移除尚未落定 + 立即重挂」之间撞 DuplicateIds。
        for child in list(chat.children):
            if child.id != "chat_anchor":
                child.remove()
        widgets: list[Any] = []
        if note:
            widgets.append(Static(Text(note, style=tui_style("notice"))))
        hidden = max(0, len(units) - self.CTX_REDRAW_MAX_UNITS)
        if hidden:
            widgets.append(
                Static(
                    Text(
                        f"… 更早 {hidden} 个单元已折叠（仅显示最后 {self.CTX_REDRAW_MAX_UNITS} 个）",
                        style=tui_style("hint"),
                    )
                )
            )
        widgets.extend(self._unit_widgets(units[hidden:]))
        if widgets:
            chat.mount_all(widgets)
        chat.scroll_end(animate=False)

    def _unit_widgets(self, units: list[ctxview.Unit]) -> list[Any]:
        """投影单元 → widget（重画的唯一渲染出口，复用事件流同款构件）"""
        out: list[Any] = []
        for unit in units:
            if unit.kind == ctxview.KIND_THINK:
                card = ThinkCard()
                card.feed(unit.text)
                card.fold()
                out.append(card)
            elif unit.kind == ctxview.KIND_ASSISTANT:
                out.append(Markdown(unit.text))
            elif unit.kind == ctxview.KIND_USER:
                out.append(Static(Text(f"{tui_glyph('prompt')} {unit.text}", style=tui_style("prompt"))))
            elif unit.kind == ctxview.KIND_TOOL_CALL:
                out.append(_tool_card({"name": unit.name, "args": unit.args}))
            elif unit.kind == ctxview.KIND_TOOL_RESULT:
                out.append(_tool_result_card({"text": unit.text}))
            else:
                # 兜底：未知角色/未知结构**必须画出来**——漏一条，视图就不再等于上下文
                out.append(Static(Text(f"[{unit.name or 'unknown'}] {unit.text}", style=tui_style("warning"))))
        return out

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
        # 0.4s 定时器可能在「屏幕已卸载 / 应用正在退出」的那一拍触发：此刻 #status
        # 与 #prompt 已不在 DOM 上，刷新既无意义又会把 NoMatches 冒泡成异常栈
        # （真实退出瞬间同样会撞到，不止测试场景）。拿不到节点就直接放弃这一拍。
        try:
            bar = self.query_one("#status", Static)
            prompt = self.query_one("#prompt", PromptInput)
        except NoMatches:
            return
        try:
            st = portal.run.status if portal.is_open else None
        except Exception:  # noqa: BLE001
            st = None
        busy = self._is_busy()
        t = Text()
        if st is not None:
            t.append(st.model, style=tui_style("status_model"))
            t.append(" · ")
            t.append(st.session_id[:8], style=tui_style("status_session"))
            t.append(" · ")
        t.append(
            f"{tui_glyph('busy')} 运行中" if busy else f"{tui_glyph('idle')} 空闲",
            style=tui_style("status_busy") if busy else tui_style("status_idle"),
        )
        if self._status_note:
            t.append(f" · {self._status_note}", style=tui_style("status_note"))
        # 差异才更新：``Static.update`` 默认 ``layout=True`` → 一次全屏重排
        # （``_arrange_root(visible_only=False)`` 遍历所有子件，O(历史块数)），
        # 空闲时每 0.4s 白烧一次。状态条是 dock+height:1 定高，改字根本不需要
        # 重排，故 diff 门 + ``layout=False`` 双保险：内容没变 → 一个字节都不动。
        if t != self._status_text:
            self._status_text = t
            bar.update(t, layout=False)
        placeholder = PROMPT_HINT_BUSY if busy else PROMPT_HINT_IDLE
        if placeholder != self._placeholder:
            self._placeholder = placeholder
            prompt.placeholder = placeholder
        # 0.4s 拍的懒对账：覆盖「没人通知我们」的改写（reload 续接、外部客户端、
        # 插件、后台任务）。快查未变时开销≈两次字典取值，空闲期可忽略。
        self._sync_view()

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
        if self._open_keys:
            self.action_open_keys()
        self.action_submit_prompt()

    async def _handle_input(self, text: str) -> None:
        action = route_input(text, self._is_busy())
        if action == "ignore":
            return
        append_history(_history_path(), text)
        self._hist.append(text)
        self._hist_idx = len(self._hist)

        if action == "reject_slash":
            self._line(
                f"{tui_glyph('reject')} 运行中已禁用斜杠命令（本轮结束后可用，Ctrl+C 可打断）",
                tui_style("warning"),
            )
            return
        if action == "queue":
            self._mount_block(
                Text(f"{tui_glyph('prompt')} {text}  {tui_glyph('queued')}已排队", style=tui_style("prompt"))
            )
            try:
                result = await portal.run.submit(text)
            except Exception as e:  # noqa: BLE001
                self._line(f"排队失败: {e}", tui_style("error"))
                return
            if result == "rejected_command":  # 理论不可达（上面已拦）
                self._line(f"{tui_glyph('reject')} 斜杠命令不入队", tui_style("warning"))
            elif result != "queued":
                self._line(f"排队结果: {result}", tui_style("warning"))
            return

        # command / send：本地发送标志同步置位（防连按双发），轮锁兜底泵交错
        self._mount_block(Text(f"{tui_glyph('prompt')} {text}", style=tui_style("prompt")))
        self._sending = True
        self._io.consumed_stream = False
        self.run_worker(self._do_round(text, command=action == "command"))

    async def _do_round(self, text: str, *, command: bool) -> None:
        resp: Any = None
        try:
            resp = await portal.run.send(text)
        except SystemExit as e:
            if str(e):
                self._line(str(e), tui_style("warning"))
            self.exit()
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 单轮失败展示而非崩溃
            self._line(f"错误: {e}", tui_style("error"))
            return
        finally:
            self._sending = False
        # 收尾拍对账：本轮任何来源（命令 / 工具 / 插件 / 外部）改了上下文，此刻落地。
        # 必须早于下面命令输出的渲染——重画会清掉事件流块，先重画再补回显才不会丢。
        self._sync_view()
        self._refresh_status()
        if resp is not None and resp.content and (command or not self._io.consumed_stream):
            # 斜杠命令输出 / 早退响应（blocked、过滤等未走流式）→ 补渲染
            self._seal_live()
            self._mount_block(Markdown(resp.content))
        self._refresh_status()

    # ── 斜杠补全提示（n7） ───────────────────────────────────

    def on_resize(self, event: Any) -> None:  # textual.events.Resize
        """窗口宽度变了 → 帮助栏按新栏宽重折行（否则会按旧宽度折好再被裁）。

        取 ``event.size`` 而不是 ``self.size``：Resize 到达时 App 自己的 size
        还是**旧值**（实测 100x30 → 34x20 时 event=34、self=100），用 self 会把
        浮层按旧宽度折好，再被新容器裁掉——正是要修的那个 bug。
        """
        if self._hint_matches:
            self._render_hint(width=int(getattr(event.size, "width", 0) or 0) or None)

    @on(TextArea.Changed, "#prompt")
    def _on_change(self, event: TextArea.Changed) -> None:
        value = event.text_area.text
        self._hint_matches = completion.slash_candidates(value) if value.startswith("/") else []
        # 正文一变选择回到第一条：候选集换了，「上次选到第 3 项」不再指向同一个东西
        self._hint_idx = 0
        self._render_hint()

    def _render_hint(self, width: int | None = None) -> None:
        """重画补全浮层：候选窗（选中行反显）+ 帮助栏。无候选 → 收起。

        Args:
            width: 显式栏宽（resize 时用事件尺寸）；None = 取当前 App 宽度。
        """
        try:
            hint = self.query_one("#hint", Horizontal)
            list_box = self.query_one("#hint_list", Static)
            detail_box = self.query_one("#hint_detail", Static)
        except NoMatches:  # 界面销毁中（Changed 可能晚于卸载）→ 静默跳过
            return
        if not self._hint_matches:
            hint.remove_class("visible")
            return

        total = len(self._hint_matches)
        self._hint_idx = max(0, min(self._hint_idx, total - 1))
        # 两栏宽度先算清楚：右栏所剩无几就改上下叠放（.narrow）
        content_w = max(8, (width or self.size.width) - 2)  # #hint 自己的左右 padding 0 1
        list_w = min(max(len(c.word) for c in self._hint_matches) + 2, HINT_LIST_MAX_W)
        narrow = content_w - list_w - 3 < HINT_DETAIL_MIN_W  # 3 = border-left + padding
        wrap_w = content_w if narrow else content_w - list_w - 3
        hint.set_class(narrow, "narrow")
        list_box.styles.width = "100%" if narrow else list_w

        rows = HINT_ROWS_NARROW if narrow else HINT_ROWS
        # 窗口跟着选择走：选中行恒在可见范围内，不必依赖 widget 内部滚动
        start = min(max(0, self._hint_idx - rows + 1), max(0, total - rows))
        body = Text()
        for i in range(start, min(total, start + rows)):
            cand = self._hint_matches[i]
            if i > start:
                body.append("\n")
            style = tui_style("completion")
            if i == self._hint_idx:
                # 选中行整行反显 + 光标记号（复用 prompt 字形：可配置、ascii 模式降级）
                style += " reverse"
                body.append(f"{tui_glyph('prompt')} ", style=style)
            else:
                body.append("  ", style=style)
            body.append(cand.word, style=style)
        detail_rows = HINT_DETAIL_ROWS_NARROW if narrow else HINT_DETAIL_ROWS
        list_box.update(body)
        detail_box.update(_wrap_cells(self._hint_detail(wrap_w), wrap_w, detail_rows))
        hint.add_class("visible")

    def _hint_detail(self, wrap_w: int) -> Text:
        """选中项的帮助文本：名字/别名、一行描述、用法正文、位置与键位提示。

        这里只拼**未折行**的富文本；折行/限行由 :func:`_wrap_cells` 按当前
        栏宽做——两件事分开，才能让行数上限与实际显示行数一致。
        """
        cand = self._hint_matches[self._hint_idx]
        # 窄栏里键位提示自己就会折成两行，占掉本来就紧的正文空间 → 用短版
        keys = "↑↓ 选择 · Tab 补全 · Esc 关闭" if wrap_w >= 40 else "↑↓ · Tab · Esc"
        text = Text()
        text.append(cand.word, style=tui_style("completion"))
        if cand.aliases:
            text.append("  " + " ".join(f"/{a}" for a in cand.aliases), style=tui_style("completion_desc"))
        if cand.desc:
            text.append("\n")
            text.append(cand.desc, style=tui_style("info"))
        if cand.help_text:
            text.append("\n")
            text.append(cand.help_text, style=tui_style("completion_desc"))
        text.append("\n")
        text.append(f"{self._hint_idx + 1}/{len(self._hint_matches)} · {keys}", style=tui_style("keys"))
        return text

    def hint_move(self, delta: int) -> bool:
        """↑↓ 在浮层里挪选择。返回 False = 浮层没开（调用方回落到历史翻页）。"""
        if not self._hint_matches:
            return False
        target = max(0, min(len(self._hint_matches) - 1, self._hint_idx + delta))
        if target != self._hint_idx:
            self._hint_idx = target
            self._render_hint()
        return True

    def dismiss_hint(self) -> None:
        """收起浮层（Esc）。正文不动——继续输入会因 Changed 自然重开。"""
        if not self._hint_matches:
            return
        self._hint_matches = []
        self._hint_idx = 0
        self._render_hint()

    def action_tab_complete(self) -> None:
        if not self._hint_matches:
            return
        idx = max(0, min(self._hint_idx, len(self._hint_matches) - 1))
        word = self._hint_matches[idx].word
        prompt = self.query_one("#prompt", PromptInput)
        prompt.text = word + " "  # 触发 Changed → 浮层自动收起（补全后无候选）
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
            if delta < 0:
                # 回溯：落在最后一行，↑ 可逐行上溯到首行后继续翻更早的历史
                prompt.move_cursor((value.count("\n"), len(value.rsplit("\n", 1)[-1])))
            else:
                # 前行：落在第一行，↓ 可逐行下行到末行后继续翻更晚的历史
                prompt.move_cursor((0, 0))

    def action_hist_prev(self) -> None:
        self._move_history(-1)

    def action_hist_next(self) -> None:
        self._move_history(1)

    # ── 动作 ─────────────────────────────────────────────────

    def action_copy_selection(self) -> None:
        """复制当前选区（输入框内或聊天区拖选），走 OSC52 写入系统剪贴板。

        只做这一件事。**打断是独立动作**（``interrupt``），各占自己的键槽——
        两个语义不再共用一个键、靠「忙不忙 / 有没有选区」运行时猜用户意图。
        """
        focused = self.screen.focused
        if isinstance(focused, TextArea) and (text := focused.selected_text):
            self.copy_to_clipboard(text)
            self.notify(f"已复制 {len(text)} 字", timeout=1.5)
            return
        if selected := self.screen.get_selected_text():
            self.screen.action_copy_text()  # 内部无选区会 SkipAction——上面已判非空
            self.screen.clear_selection()
            self.notify(f"已复制 {len(selected)} 字", timeout=1.5)
            return
        self.notify("未选中文本", timeout=2.0)

    def action_interrupt(self) -> None:
        """打断当前操作：思考中保留已生成内容，工具执行中标记当前工具失败。

        空闲时给出提示而非静默——用户按了键总该知道发生了什么。
        """
        if self._is_busy() and portal.is_open:
            portal.run.cancel()
            self._line(f"{tui_glyph('stop')} 已请求打断", tui_style("warning"))
            self._refresh_status()
            return
        self.notify("当前没有运行中的操作", timeout=2.0)

    def action_app_quit(self) -> None:
        self.exit()

    def action_stats(self) -> None:
        self.push_screen(StatsModal(build_stats_body()))

    # ── 消息区：滚轮与点击卡片的键盘等价 ─────────────────────
    #
    # 这几个动作用 priority 绑定且直接操作 #chat，不依赖焦点——键盘可达的
    # 意义正是「焦点在输入框里也能操作消息区」。输入框最多 6 行，把它内建
    # 的 PgUp/Ctrl+N 让位给消息区几乎无损失。

    def _cards(self) -> list[Collapsible]:
        """消息区内的可折叠卡片，按 DOM 顺序（= 对话顺序）。"""
        return [child for child in self._chat().children if isinstance(child, Collapsible)]

    def _card_of(self, widget: Widget | None) -> Collapsible | None:
        """从焦点 widget 向上找到所属卡片（焦点通常落在 CollapsibleTitle 上）。"""
        node: Widget | None = widget
        while node is not None:
            if isinstance(node, Collapsible):
                return node
            parent = node.parent
            node = parent if isinstance(parent, Widget) else None
        return None

    def _focus_card(self, card: Collapsible) -> None:
        """滚动到卡片并聚焦其标题栏——随后 Enter 即可开合。"""
        card.scroll_visible(animate=False)
        titles = card.query("CollapsibleTitle")
        if len(titles):
            titles.first().focus()

    def action_scroll_page_up(self) -> None:
        self._chat().scroll_page_up(animate=False)

    def action_scroll_page_down(self) -> None:
        self._chat().scroll_page_down(animate=False)

    def action_jump_latest(self) -> None:
        self._chat().scroll_end(animate=False)

    def action_card_next(self) -> None:
        cards = self._cards()
        if not cards:
            return
        current = self._card_of(self.focused)
        # 无聚焦卡片时视作「在末尾之前」→ 第一次按跳到最后一张
        index = cards.index(current) if current in cards else -1
        self._focus_card(cards[min(index + 1, len(cards) - 1)])

    def action_card_prev(self) -> None:
        cards = self._cards()
        if not cards:
            return
        current = self._card_of(self.focused)
        index = cards.index(current) if current in cards else len(cards)
        self._focus_card(cards[max(index - 1, 0)])

    def action_card_toggle(self) -> None:
        card = self._card_of(self.focused)
        if card is None:
            cards = self._cards()
            card = cards[-1] if cards else None
        if card is not None:
            card.collapsed = not card.collapsed

    def action_cards_toggle_all(self) -> None:
        """有展开的卡片 → 全折；否则全展（连按即在两种全态间切换）。"""
        cards = self._cards()
        if not cards:
            return
        collapse = any(not card.collapsed for card in cards)
        for card in cards:
            card.collapsed = collapse


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
    open_keys: bool = False,
) -> None:
    """运行 TUI 直至退出（main() 的 finally 负责 ctl.close）。"""
    app = FPApp(io, directive=directive, resume=resume, open_keys=open_keys)
    install_theme(app)  # config.json tui 段：内置主题名/palette，坏数据静默回落
    install_appearance()  # 语义样式/符号表：tui.styles/symbols/ascii，坏数据静默回落
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
