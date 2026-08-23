"""
display.py — FP 显示模块（fp-terminal 版）

将 6 类输出 (A操作反馈/B行为提示/C异常警示/D LLM流/E系统日志/🎨仪式感)
统一着色输出到终端。所有颜色、样式、截断长度均从 config.json
的 display_styles / display_truncation 按名称读取。
"""

import asyncio
import contextlib
import json
import os
import sys
import time

from fp_cli.style import apply_style, color_supported, truncate

# ── 静默模式：子 agent 执行时抑制所有终端输出 ──────────
_FP_SILENT = os.environ.get("FP_SUBAGENT_SILENT") == "1"


def _silent() -> bool:
    """返回 True 表示当前为子 agent 静默模式，应跳过所有终端 UI 输出"""
    return _FP_SILENT


# ═══════════════════════════════════════════════════════════
# A. 操作反馈 — 用户主动操作后的回应
#   注册名称: "info", "item"
# ═══════════════════════════════════════════════════════════


def info(msg: str):
    """一般信息 / 操作成功的反馈（消息中应包含 emoji）"""
    if _silent():
        return
    print(apply_style(msg, "info"))


def item(msg: str):
    """列表中的子条目（默认色，无着色，由调用方控制缩进）"""
    if _silent():
        return
    print(msg)


# ═══════════════════════════════════════════════════════════
# B. 行为提示 — 系统主动给出的引导信息
#   注册名称: "hint"
# ═══════════════════════════════════════════════════════════


def hint(msg: str):
    """引导 / 用法提示（消息中应包含 💡）"""
    if _silent():
        return
    print(apply_style(msg, "hint"))


def divider(char: str = "─"):
    """青色全宽隔离线（撑满终端宽度），用于分隔输入与回复区块"""
    if _silent():
        return
    try:
        width = os.get_terminal_size().columns
    except OSError:
        width = 80
    print(apply_style(char * width, "hint"))


# ═══════════════════════════════════════════════════════════
# C. 异常警示 — 非预期路径
#   注册名称: "error", "warning"
#   铁律: error() 调用方应在 msg 外再传 fix 参数给出解决指引。
# ═══════════════════════════════════════════════════════════


def error(msg: str, fix: str = ""):
    """错误（配色从配置），可带第二行解决指引"""
    if _silent():
        return
    print(apply_style(msg, "error"))
    if fix:
        print(apply_style(f"   → {fix}", "hint"))


def warning(msg: str):
    """警告（配色从配置）"""
    if _silent():
        return
    print(apply_style(msg, "warning"))


# ═══════════════════════════════════════════════════════════
# D. LLM 流 — 大模型的思考、工具调用、最终回复
#   注册名称: "llm_thought", "llm_tool", "llm_output"
#   支持按名称截断：llm_thought / llm_tool 可配置 truncation
# ═══════════════════════════════════════════════════════════


def llm_thought(msg: str, end: str = "\n"):
    """LLM 思考过程（支持按配置截断）"""
    if _silent():
        return
    text = truncate(msg, "llm_thought")
    print(apply_style(text, "llm_thought"), end=end, flush=True)


def llm_tool(msg: str):
    """工具调用 / 工具结果（支持按配置截断）"""
    if _silent():
        return
    text = truncate(msg, "llm_tool")
    print(apply_style(text, "llm_tool"))


# ── 工具调用格式化：单行折叠 + 项目级截断 ─────────────────────────────
# 参数值超长时只截断超长项目（保持 JSON 结构/key 完整），
# 而非从中间砍断整个字符串。

_STR_MAX = 60  # 单个字符串值最大长度
_STRUCT_MAX = 4000  # 嵌套 dict/list 展开后整体最大长度
_TOOL_PART_DELAY = 0.02  # 工具调用行逐段流式展开的间隔（秒）


def _trunc_str(s: str, limit: int) -> str:
    if len(s) <= limit:
        return repr(s)
    return repr(s[:limit]) + f"… <+{len(s) - limit} chars>"


def _trunc_struct(s: str, limit: int) -> str:
    """截断已展开的结构内文（不套 repr 引号，保持与顶层一致的 key=value 风格）"""
    if len(s) <= limit:
        return s
    return s[:limit] + f"… <+{len(s) - limit} chars>"


def _fmt_tool_value(v, max_str: int = _STR_MAX, max_struct: int = _STRUCT_MAX) -> str:
    """递归格式化工具参数值：长字符串按项目截断，保持 dict/list 结构。

    嵌套 dict/list 展开后整体超长时仅截断内文并标注，保留 {}/[] 结构括号与
    key，使嵌套结构呈现与顶层参数一致的 ``key=value`` 风格（无多余引号）。
    """
    if isinstance(v, str):
        return _trunc_str(v, max_str)
    if v is None or isinstance(v, (bool, int, float)):
        return repr(v)
    if isinstance(v, dict):
        inner = ", ".join(f"{k}={_fmt_tool_value(x, max_str, max_struct)}" for k, x in v.items())
        return f"{{{_trunc_struct(inner, max_struct)}}}"
    if isinstance(v, (list, tuple)):
        inner = ", ".join(_fmt_tool_value(x, max_str, max_struct) for x in v)
        return f"[{_trunc_struct(inner, max_struct)}]"
    return _trunc_str(str(v), max_str)


def _tool_parts(name: str, args) -> list[str]:
    """将工具调用拆分为可流式展示的片段：前缀 → 各参数 → 后缀。

    args 可为 dict（已解析）或字符串（未解析/流式不完整 JSON）。
    解析失败时降级为单段（原始 JSON 经结构级截断），不因结构不完整而崩溃。
    """
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            return [f"  🛠️  {name}({_trunc_struct(args, _STRUCT_MAX)})"]
    if not isinstance(args, dict):
        return [f"  🛠️  {name}({args})"]

    segs = [f"  🛠️  {name}(\n"]
    for i, (k, v) in enumerate(args.items()):
        if i:
            segs.append(", \n")
        segs.append(f"{k}={_fmt_tool_value(v)}")
    segs.append("\n)")
    return segs


def format_tool_call(name: str, args) -> str:
    """将工具调用格式化为单行可读形式（供一次性打印场景使用）。"""
    return "".join(_tool_parts(name, args))


def llm_output(text: str):
    """LLM 回复内容（流式，默认色，不换行）"""
    if _silent():
        return
    print(text, end="", flush=True)


class LLMStreamer:
    """LLM 流式输出管理器

    封装思考/回复切换的 ANSI 状态管理，调用方只需传入 token。
    自动处理思考标记、灰色着色、模式切换时的重置。

    用法:
        stream = LLMStreamer(silent=False)
        for chunk in response:
            if chunk.thinking:
                stream.think(chunk.thinking)
            if chunk.content:
                stream.content(rendered_content)
        stream.end()
    """

    # 类级锁：跨实例共享（streamer 可能被重建），串行化工具调用/结果输出，
    # 避免并行工具调用时多个 tool() 协程片段交错挤在同一行。
    _tool_lock: asyncio.Lock | None = None

    @classmethod
    def _get_tool_lock(cls) -> asyncio.Lock:
        if cls._tool_lock is None:
            cls._tool_lock = asyncio.Lock()
        return cls._tool_lock

    def __init__(self, silent: bool = False):
        self.silent = silent or _FP_SILENT
        self._thinking = False
        self._has_content = False
        self._buffer = ""
        self.content = ""  # 最终内容
        self.thinking = ""  # 思考内容
        self._live = None  # rich.live.Live 实例，首次内容时创建

    @staticmethod
    def _safe_print(*args, **kwargs):
        """安全打印，stdout 不可用时静默忽略"""
        with contextlib.suppress(AttributeError, ValueError, OSError):
            print(*args, **kwargs)

    def _ensure_live(self):
        """惰性创建 rich.live.Live，用于流式 Markdown 渲染"""
        if self._live is not None:
            return
        try:
            from rich.live import Live
            from rich.markdown import Markdown

            self._live = Live(
                Markdown(self._buffer),
                refresh_per_second=10,
                vertical_overflow="visible",
            )
            self._live.__enter__()
        except Exception:
            self._live = None

    def _clear_live(self):
        """清空 Live 画布内容。

        rich Live 默认 (transient=False) 在 stop 时会保留最后一帧，因此
        需要清空思考/内容后再退出，否则思考行会残留在屏幕上。
        """
        if self._live is not None:
            try:
                from rich.text import Text

                self._live.update(Text(""))
            except Exception:
                pass

    def _exit_live(self):
        """安全退出 Live 上下文"""
        if self._live is not None:
            with contextlib.suppress(Exception):
                self._live.__exit__(None, None, None)
            self._live = None

    def reset(self):
        """重置流式状态，用于异常恢复"""
        self._exit_live()
        self._thinking = False
        self._has_content = False
        self._buffer = ""
        self.content = ""
        self.thinking = ""

    def think(self, text: str):
        """输出思考 token（Live 流式渲染思考配色，切换到内容时自动清除）"""
        if self.silent:
            self.thinking += text
            return
        if not text:
            return

        self._thinking = True
        self.thinking += text

        # 使用 Live 渲染思考内容（配色从 config.json llm_thought）
        if not self._live:
            self._ensure_live()
        if self._live:
            try:
                from rich.text import Text

                self._live.update(Text(self.thinking, style=self._think_rich_style()))
            except Exception:
                pass
        else:
            # Live 不可用时回退到 print
            if not hasattr(self, "_thought_prefix"):
                prefix = "\n" if self._has_content else ""
                self._safe_print(apply_style(f"{prefix}思考: ", "llm_thought"), end="", flush=True)
                self._thought_prefix = True
            llm_thought(text, end="")

    @staticmethod
    def _think_rich_style() -> str:
        """从 config.json 读取 llm_thought 样式，构建 rich 风格字符串"""
        import json
        import os

        from fp_core.platform_utils import get_config_dir

        path = os.path.join(get_config_dir(), "config.json")
        raw = {}
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f).get("display_styles", {}).get("llm_thought", {})
            except Exception:
                pass

        parts = []
        color = raw.get("color", "default")
        if color and color != "default":
            parts.append(color)
        if raw.get("bold"):
            parts.append("bold")
        if raw.get("dim"):
            parts.append("dim")
        if raw.get("italic"):
            parts.append("italic")
        return " ".join(parts) if parts else ""

    def write(self, text: str):
        """实时流式 Markdown 渲染内容 token

        从思考阶段首次进入内容阶段时，自动清除 Live 画布上的思考内容。
        """
        if self.silent:
            self._buffer += text
            self._has_content = True
            self.content += text
            return
        if not text:
            return

        # 从思考阶段切换到内容阶段：清空 Live 画布
        if self._thinking:
            self._thinking = False
            self._clear_live()

        self._buffer += text
        self._has_content = True

        if not self._live:
            self._ensure_live()
        if self._live:
            try:
                from rich.markdown import Markdown

                self._live.update(Markdown(self._buffer))
            except Exception:
                pass

    async def tool(self, name: str, args) -> None:
        """工具调用阶段：覆盖思考信息，逐段流式展开格式化工具行。

        通过类级锁串行化输出：并行工具调用时每个工具独占一行、片段不交错，
        先后调用的工具行按顺序依次完整展示。进入工具调用时若正在思考
        （Live 画布），先清空并退出 Live，避免思考内容与工具行纠缠；
        之后若有新思考会重新创建 Live。
        """
        if self.silent:
            return
        async with LLMStreamer._get_tool_lock():
            if self._live is not None:
                # 先清空思考画布再退出（rich Live 默认保留最后一帧，
                # 否则调用工具前的思考会残留覆盖不到）
                self._clear_live()
                self._exit_live()
            self._thinking = False
            segs = _tool_parts(name, args)
            styled_segs = [apply_style(seg, "llm_tool") for seg in segs]
            for i, seg in enumerate(styled_segs):
                self._safe_print(seg, end="", flush=True)
                if i < len(segs) - 1:
                    await asyncio.sleep(_TOOL_PART_DELAY)
            self._safe_print()  # 换行

    async def tool_result_line(self, result: str) -> None:
        """工具结果行：与工具行共用同一把锁串行输出。

        保证结果不会插入尚未换行的工具行中间；并行工具的结果按完成顺序
        依次独占一行展示（截断规则与 llm_tool 配置一致）。
        """
        if self.silent:
            return
        async with LLMStreamer._get_tool_lock():
            text = truncate(f"  📋  {result.strip()}", "llm_tool")
            self._safe_print(apply_style(text, "llm_tool"))

    def end(self, interrupted: bool = False):
        """结束流式输出

        若有 Live 则退出（最后一帧保留屏幕）；若只有思考且无 Live，
        关闭思考行。异常中断时保留 Live 最后一帧。
        """
        if self.silent:
            return
        if interrupted:
            self._exit_live()
            self._safe_print(apply_style("⏹️ 已中断", "yellow_bold"))
            return

        if self._live:
            # 只有纯思考（尚未输出内容）时先清空再退出：
            # rich Live 默认保留最后一帧，否则思考残留屏幕
            # （如思考→工具调用，stream_end() 退出后 tool() 会重建新 streamer）；
            # 若已输出内容则保留最后一帧，不清空。
            if not self._has_content:
                self._clear_live()
            self._exit_live()
        elif self._thinking:
            # 清空思考行（防止残留空行）

            clear = "\r" + " " * (max(len(self.thinking), 10)) + "\r"
            self._safe_print(clear)

        self._buffer = ""
        self._has_content = False

    @staticmethod
    def _render_markdown(text: str):
        """用 rich 渲染 Markdown，缺失时降级为纯文本"""
        try:
            from rich.console import Console
            from rich.markdown import Markdown

            Console().print(Markdown(text))
        except ImportError:
            LLMStreamer._safe_print(text)
        except (AttributeError, ValueError, OSError):
            pass


# ═══════════════════════════════════════════════════════════
# E. 系统日志 — 开发者调试用，默认隐藏
# ═══════════════════════════════════════════════════════════
# 🎨 仪式感 — 品牌记忆点
#   注册名称: "startup", "shutdown_panel", "logo"
# ═══════════════════════════════════════════════════════════


def startup(model: str, resume: bool = False):
    """启动横幅"""
    if _silent():
        return
    if resume:
        print(apply_style(f"🤖 FP 已续会话 (模型: {model})", "startup"))
    else:
        print(apply_style(f"🤖 FP 已启动 (模型: {model})", "startup"))
    print()


def _display_width(text: str) -> int:
    """返回字符串在终端中的实际显示宽度（全宽=2，半宽=1）"""
    try:
        from wcwidth import wcswidth

        w = wcswidth(text)
        return w if w >= 0 else len(text)
    except ImportError:
        return len(text)


def shutdown_panel(
    summary: str, file: str, model: str, msg_count: int, created: str, duration: str = "", token_usage=None
):
    """退出时的统计面板（框线装饰），自动适应内容宽度"""
    if _silent():
        return
    MIN_W = 48
    MAX_W = 60

    token_text = ""
    if token_usage is not None and token_usage.call_count > 0:
        base = f"Token: {token_usage.total_tokens:,} (↑{token_usage.prompt_tokens:,} ↓{token_usage.completion_tokens:,}"
        if token_usage.cache_hit_tokens or token_usage.cache_miss_tokens:
            base += f" cache:{token_usage.cache_hit_rate_str}"
        base += f" ×{token_usage.call_count})"
        token_text = base

    entries: list[tuple[str, str]] = [
        ("📂  会话结束", "header"),
        ("", "sep"),
        (f"总结: {summary}", "info"),
        (f"文件: {file}", "info"),
        ("", "sep"),
        ("📊  统计信息", "header"),
        ("", "sep"),
        (f"模型: {model}", "info"),
        (f"消息: {msg_count} 条", "info"),
    ]
    if token_text:
        entries.append((token_text, "info"))
    entries.append((f"创建: {created}", "info"))
    if duration:
        entries.append((f"耗时: {duration}", "info"))

    max_text_width = max(_display_width(text) for text, _ in entries)
    W = min(max(MIN_W, max_text_width + 6), MAX_W)
    text_w = W - 6
    sep = "─" * (W - 2)

    def tb(text: str) -> str:
        if _display_width(text) <= text_w:
            pad = text_w - _display_width(text)
            return f"║  {text}{' ' * pad}  ║"
        else:
            result = ""
            for ch in text:
                candidate = result + ch
                if _display_width(candidate + "…") > text_w:
                    break
                result = candidate
            display = result + "…"
            pad = text_w - _display_width(display)
            return f"║  {display}{' ' * pad}  ║"

    def sep_line() -> str:
        return f"║{sep}║"

    print(f"\n╔{sep}╗")
    for text, typ in entries:
        if typ == "sep":
            print(sep_line())
        else:
            print(tb(text))
    print(f"╚{sep}╝")
    print()


def _logo_gradient_color(ratio: float) -> str:
    """从暖橙 (top) 到冷紫 (bottom) 的渐变色 ANSI true color 转义码"""
    r = int(255 * (1 - ratio) + 107 * ratio)
    g = int(107 * (1 - ratio) + 91 * ratio)
    b = int(53 * (1 - ratio) + 255 * ratio)
    return f"\033[38;2;{r};{g};{b}m"


_KEYGEN_FRAME_MS = 35
_KEYGEN_STEP = 1.0
_KEYGEN_GLOW_RADIUS = 5


def _scan_color(i: int, scan_line: float, total: int) -> str:
    """扫描光带颜色函数"""
    dist = abs(i - scan_line)
    radius = _KEYGEN_GLOW_RADIUS
    if dist <= radius:
        t = dist / radius
        r = int(255 * (1 - t) + 40 * t)
        g = int(200 * (1 - t) + 60 * t)
        b = int(60 * (1 - t) + 130 * t)
    else:
        r, g, b = 12, 16, 38
    return f"\033[38;2;{r};{g};{b}m"


def _build_startup_frame(model: str, resume: bool) -> tuple[list[str], int]:
    """构建启动信息面板的文本行"""
    try:
        from importlib.metadata import version as _meta_version

        ver = _meta_version("fp-core")
    except Exception:
        ver = "?"

    model_display = model if model else "(none)"
    status = "Resume Session" if resume else "New Session"
    ver_str = f"v{ver}"
    title = "FP"

    PANEL_W = 50
    BORDER = "  +" + "=" * (PANEL_W - 4) + "+"

    def content_line(text: str) -> str:
        inner = PANEL_W - 4
        left = (inner - len(text)) // 2
        right = inner - left - len(text)
        return f"  |{' ' * left}{text}{' ' * right}|"

    lines = [
        BORDER,
        content_line(""),
        content_line(f"{title}  {ver_str}"),
        content_line(""),
        content_line("=====  Initializing  ====="),
        content_line(""),
        content_line(f"Model:  {model_display}"),
        content_line(f"Status: {status}"),
        content_line(""),
        BORDER,
    ]
    max_w = max(len(line) for line in lines)
    return lines, max_w


def _print_logo_static(lines: list[str], total: int):
    """打印静态渐变色面板（暖橙→冷紫逐行渐变）"""
    for i, line in enumerate(lines):
        ratio = i / max(total - 1, 1)
        ansi = _logo_gradient_color(ratio)
        print(f"{ansi}{line}\033[0m")


def print_logo(model: str = "", resume: bool = False):
    """启动动画：一束暖金光带扫描面板，逐行揭示启动信息"""
    if _silent():
        return

    lines, max_w = _build_startup_frame(model, resume)
    total = len(lines)
    use_color = color_supported()
    use_anim = use_color and sys.stdout.isatty()

    if not use_anim:
        _print_logo_static(lines, total)
        print()
        return

    try:
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()
        scan = -2.0
        scan_end = total + 2.0

        for i, line in enumerate(lines):
            ansi = _scan_color(i, scan, total)
            sys.stdout.write(ansi + line + "\033[0m\n")
        sys.stdout.flush()
        time.sleep(_KEYGEN_FRAME_MS / 1000)

        while True:
            scan += _KEYGEN_STEP
            if scan >= scan_end:
                break
            sys.stdout.write(f"\033[{total}A")
            for i, line in enumerate(lines):
                ansi = _scan_color(i, scan, total)
                sys.stdout.write(ansi + line.ljust(max_w) + "\033[0m\n")
            sys.stdout.flush()
            time.sleep(_KEYGEN_FRAME_MS / 1000)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()

    sys.stdout.write(f"\033[{total}A")
    _print_logo_static(lines, total)
    print()


# ═══════════════════════════════════════════════════════════
# F. Spinner — 异步等待动画
# ═══════════════════════════════════════════════════════════


class Spinner:
    """异步 spinner 动画"""

    def __init__(self, message: str = "思考中"):
        self._message = message
        self._task = None
        self._chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    async def start(self):
        """启动 spinner（启动一个后台 asyncio 任务）"""
        if _silent():
            self._task = None
            return
        self._task = asyncio.create_task(self._spin())

    async def _spin(self):
        """后台旋转动画"""
        idx = 0
        try:
            while True:
                char = self._chars[idx % len(self._chars)]
                msg = f"\r{char} {self._message}..."
                self._safe_stdout_write(msg)
                idx += 1
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            self._safe_stdout_write("\r" + " " * (len(self._message) + 6) + "\r")

    @staticmethod
    def _safe_stdout_write(msg: str) -> None:
        try:
            sys.stdout.write(msg)
            sys.stdout.flush()
        except (AttributeError, ValueError, OSError):
            pass

    async def stop(self):
        """停止 spinner 并清除动画行"""
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
