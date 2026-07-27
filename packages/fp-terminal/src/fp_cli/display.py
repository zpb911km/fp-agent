"""
display.py — Five Pebbles 显示模块（fp-terminal 版）

将 6 类输出 (A操作反馈/B行为提示/C异常警示/D LLM流/E系统日志/🎨仪式感)
统一着色输出到终端。所有颜色、样式、截断长度均从 config.json
的 display_styles / display_truncation 按名称读取。
"""

import asyncio
import contextlib
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

    def __init__(self, silent: bool = False):
        self.silent = silent
        self._thinking = False
        self._has_content = False
        self._buffer = ""
        self.content = ""  # 最终内容
        self.thinking = ""  # 思考内容

    @staticmethod
    def _safe_print(*args, **kwargs):
        """安全打印，stdout 不可用时静默忽略"""
        with contextlib.suppress(AttributeError, ValueError, OSError):
            print(*args, **kwargs)

    def reset(self):
        """重置流式状态，用于异常恢复"""
        self._thinking = False
        self._has_content = False
        self._buffer = ""
        self.content = ""
        self.thinking = ""

    def think(self, text: str):
        """输出思考 token（配色从配置），首次自动显示思考标记"""
        if self.silent:
            self.thinking += text
            return
        if not text:
            return
        if not self._thinking:
            prefix = "\n" if self._has_content else ""
            self._safe_print(apply_style(f"{prefix}思考: ", "llm_thought"), end="", flush=True)
            self._thinking = True

        llm_thought(text, end="")
        self.thinking += text

    def write(self, text: str):
        """缓冲回复内容，等待 end() 时统一用 rich Markdown 渲染"""
        if self.silent:
            self._buffer += text
            self._has_content = True
            self.content += text
            return
        if not text:
            return
        if self._thinking:
            self._safe_print()
            self._thinking = False
        self._buffer += text
        self._has_content = True

    def end(self, interrupted: bool = False):
        """结束流式输出，用 rich 渲染完整的 Markdown 内容"""
        if self.silent:
            return
        if interrupted:
            self._safe_print(apply_style("⏹️ 已中断", "yellow_bold"))
            return
        if self._thinking:
            self._safe_print(apply_style("", "llm_thought"))
        elif self._has_content and self._buffer:
            self._render_markdown(self._buffer)
            self._buffer = ""
        elif self._has_content:
            self._safe_print()
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
        print(apply_style(f"🤖 Five Pebbles 已续会话 (模型: {model})", "startup"))
    else:
        print(apply_style(f"🤖 Five Pebbles 已启动 (模型: {model})", "startup"))
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
    print("👋  再见！")


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
    title = "Five Pebbles"

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
