"""
display.py — Five Pebbles 显示模块

将 6 类输出 (A操作反馈/B行为提示/C异常警示/D LLM流/E系统日志/🎨仪式感)
统一着色输出到终端。

特性：
- ANSI 16 色，兼容所有现代终端
- 支持 NO_COLOR 环境变量（管道模式自动禁用颜色）
- 通过 DEBUG 环境变量控制系统日志可见性
"""

import os
import sys

# ── ANSI 转义码 ──────────────────────────────────────────
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_ITALIC = "\033[3m"

# 16 色前景
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"
_WHITE = "\033[37m"
_BRIGHT_BLACK = "\033[35m"
_BRIGHT_RED = "\033[91m"


def _use_color() -> bool:
    """检测终端是否支持颜色"""
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    return True


def _c(text: str, color: str, bold: bool = False,
       dim: bool = False, italic: bool = False) -> str:
    """应用 ANSI 颜色到文本（内部工具函数）"""
    if not _use_color():
        return text
    prefix = color
    if bold:
        prefix += _BOLD
    if dim:
        prefix += _DIM
    if italic:
        prefix += _ITALIC
    return f"{prefix}{text}{_RESET}"


# ═══════════════════════════════════════════════════════════
# A. 操作反馈 — 用户主动操作后的回应
#   颜色: 绿色。成功、信息、列表项统一用此分类。
# ═══════════════════════════════════════════════════════════

def info(msg: str):
    """一般信息 / 操作成功的反馈（消息中应包含 emoji）"""
    print(_c(msg, _GREEN))


def item(msg: str):
    """列表中的子条目（默认色，无着色，由调用方控制缩进）"""
    print(msg)


# ═══════════════════════════════════════════════════════════
# B. 行为提示 — 系统主动给出的引导信息
#   颜色: 青色。温柔，不打断阅读节奏。
# ═══════════════════════════════════════════════════════════

def hint(msg: str):
    """引导 / 用法提示（消息中应包含 💡）"""
    print(_c(msg, _CYAN))


# ═══════════════════════════════════════════════════════════
# C. 异常警示 — 非预期路径
#   颜色: 错误→亮红粗体，警告→黄色。
#   铁律: error() 调用方应在 msg 外再传 fix 参数给出解决指引。
# ═══════════════════════════════════════════════════════════

def error(msg: str, fix: str = ""):
    """错误（亮红粗体），可带第二行解决指引"""
    print(_c(msg, _BRIGHT_RED, bold=True))
    if fix:
        print(_c(f"   → {fix}", _BRIGHT_BLACK))


def warning(msg: str):
    """警告（黄色）"""
    print(_c(msg, _YELLOW))


# ═══════════════════════════════════════════════════════════
# D. LLM 流 — 大模型的思考、工具调用、最终回复
#   思考: 暗淡斜体，退到背景层
#   工具: 黄色，与正常文本区分
#   回复: 默认色，原样输出（阅读主体）
# ═══════════════════════════════════════════════════════════

def llm_thought(msg: str):
    """LLM 思考过程（灰色）"""
    print(_c(msg, _BRIGHT_BLACK, dim=True))


def llm_tool(msg: str):
    """工具调用 / 工具结果"""
    print(_c(msg, _YELLOW))


def llm_output(text: str):
    """LLM 回复内容（流式，默认色，不换行）"""
    print(text, end="", flush=True)


def llm_newline():
    """LLM 回复结束后的换行"""
    print()


def llm_iteration(count: int):
    """打印迭代次数统计"""
    print(_c(f"📊 本次交互共迭代 {count} 次", _BRIGHT_BLACK, dim=True))
    print()


class LLMStreamer:
    """LLM 流式输出管理器
    
    封装思考/回复切换的 ANSI 状态管理，调用方只需传入 token。
    自动处理 ┌思考┐ 标记、灰色着色、模式切换时的重置。
    
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

    def think(self, text: str):
        """输出思考 token（灰色），首次自动显示 ┌思考┐ 标记"""
        if self.silent or not text:
            return
        if not self._thinking:
            # 如果之前输出过内容，先换行再显示思考标记
            prefix = "\n" if self._has_content else ""
            print(_c(f"{prefix}思考: ", _BRIGHT_BLACK, dim=True), end="", flush=True)
            self._thinking = True
        print(_c(text, _BRIGHT_BLACK, dim=True), end="", flush=True)

    def content(self, text: str):
        """输出回复 token（默认色），从思考切换时自动重置颜色"""
        if self.silent or not text:
            return
        if self._thinking:
            print()  # 重置 + 换行
            self._thinking = False
        print(text, end="", flush=True)
        self._has_content = True

    def end(self):
        """结束流式输出，确保颜色重置 + 最终换行"""
        if self.silent:
            return
        if self._thinking:
            print(_c("", _BRIGHT_BLACK))  # 重置并换行
        elif self._has_content:
            print()  # 内容结束换行


# ═══════════════════════════════════════════════════════════
# E. 系统日志 — 开发者调试用，默认隐藏
#   仅在设置了 DEBUG 环境变量时输出。
# ═══════════════════════════════════════════════════════════

def debug(msg: str):
    """调试日志（暗淡灰色），仅 DEBUG=1 时可见"""
    if os.environ.get("DEBUG"):
        print(_c(f"┐dbg│ {msg}", _BRIGHT_BLACK, dim=True))


# ═══════════════════════════════════════════════════════════
# 🎨 仪式感 — 品牌记忆点
#   启动/退出时的装饰性输出，使用青+粗体 + 框线。
# ═══════════════════════════════════════════════════════════

def startup(model: str):
    """启动横幅"""
    print(_c(f"🤖 Five Pebbels 已启动 (模型: {model})", _CYAN, bold=True))
    print()


def shutdown_panel(summary: str, file: str, model: str,
                   msg_count: int, created: str,
                   duration: str = ""):
    """退出时的统计面板（框线装饰）"""
    W = 48
    sep = "─" * W

    def tb(text: str) -> str:
        return f"║  {text:<{W + 4}s}║"

    print(f"\n╔{sep}╗")
    print(tb("📂  会话结束"))
    print(f"║{sep}║")
    print(tb(f"总结: {summary}"))
    print(tb(f"文件: {file}"))
    print(f"║{sep}║")
    print(tb("📊  统计信息"))
    print(f"║{sep}║")
    print(tb(f"模型: {model}"))
    print(tb(f"消息: {msg_count} 条"))
    print(tb(f"创建: {created}"))
    if duration:
        print(tb(f"耗时: {duration}"))
    print(f"╚{sep}╝")
    print()
    print("👋  再见！")


LOGO_ART = r'''
::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
:::::::::::::::::::::::::,,,,,,,,,,:::::::::::::::::::::::::
::::::::::::::::::::::,,,,         ,,,::::::::::::::::::::::
::::::::::::::::::::,,  ;)XYYYYYYUn+   ,::::::::::::::::::::
::::::::::::::::::, ,[uzccvvvvvvvvvcczf> ,::::::::::::::::::
:::::::::::,  ,:: ,?rcXXv]        l/XXzv\i ,::,  ,::::::::::
:::::::::,,~|}I ,_/vX1,               irXx1! ,<|{I,:::::::::
:::::::::, |oLi  rMXI                   ]bp_  )hO< ,::::::::
:::::::::, |oL>i1uL\:                   ~cJt?;1aO< ,::::::::
:::::::::, |oC>/oY!                      ,}dZ~{hO< ,::::::::
:::::::::, |ow|XMYl                       [bktn*0< ,::::::::
:::::::::, |oWdo8Yl                       [b&bh80< ,::::::::
:::::::::, |o0?x#Yl  l<~<I        ,i~~>:  [bp}\o0< ,::::::::
:::::::::, |oC>t*Yl :ja#kt,       <J##Z?  [bw+{hO< ,::::::::
:::::::::, |oL>t*Yl ;v%$&x:       +Z$$b}  [bw_1hO< ,::::::::
:::::::::, \oL>t*Yl ;u8$&r:       ~Z$$b}  [bw+1aO< ,::::::::
:::::::::, (kC+r#Yl ;nM%*j:       ~Q88q]  [bq])bL< ,::::::::
:::::::::,,!]jQhWXi ,>[}]i        :+}}-I ,}dMpY(+;,:::::::::
:::::::::::, >{()nL\:                   ~cJt)(?; ,::::::::::
:::::::::::::,   r#Xl                   ]bp_   ,::::::::::::
:::::::::::::::,,_/vX1,               >rXx1!,,::::::::::::::
::::::::::::::::: ,?rcXYv]        l/XXzv\! ,::::::::::::::::
::::::::::::::::::, ,[ucccvcvvvvvcccccf> ,::::::::::::::::::
::::::::::::::::::::,,  :)zYYYYYYYn+   ,::::::::::::::::::::
::::::::::::::::::::::::,,         ,:,::::::::::::::::::::::
:::::::::::::::::::::::::,,,,,,,,,,:::::::::::::::::::::::::
::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
::::::::::::::::::::::::Five Pebbels::::::::::::::::::::::::
::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
'''


def print_logo():
    """打印 ASCII 启动画"""
    print(LOGO_ART)
