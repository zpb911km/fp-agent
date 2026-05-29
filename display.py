"""
display.py — Five Pebbels 显示模块

将 6 类输出 (A操作反馈/B行为提示/C异常警示/D LLM流/E系统日志/🎨仪式感)
统一着色输出到终端。所有颜色、样式、截断长度均从 config.json
的 display_styles / display_truncation 按名称读取。
"""

import os
import sys

from config import apply_style, truncate


# ═══════════════════════════════════════════════════════════
# A. 操作反馈 — 用户主动操作后的回应
#   注册名称: "info", "item"
# ═══════════════════════════════════════════════════════════

def info(msg: str):
    """一般信息 / 操作成功的反馈（消息中应包含 emoji）"""
    print(apply_style(msg, "info"))


def item(msg: str):
    """列表中的子条目（默认色，无着色，由调用方控制缩进）"""
    print(msg)


# ═══════════════════════════════════════════════════════════
# B. 行为提示 — 系统主动给出的引导信息
#   注册名称: "hint"
# ═══════════════════════════════════════════════════════════

def hint(msg: str):
    """引导 / 用法提示（消息中应包含 💡）"""
    print(apply_style(msg, "hint"))


# ═══════════════════════════════════════════════════════════
# C. 异常警示 — 非预期路径
#   注册名称: "error", "warning"
#   铁律: error() 调用方应在 msg 外再传 fix 参数给出解决指引。
# ═══════════════════════════════════════════════════════════

def error(msg: str, fix: str = ""):
    """错误（配色从配置），可带第二行解决指引"""
    print(apply_style(msg, "error"))
    if fix:
        print(apply_style(f"   → {fix}", "hint"))


def warning(msg: str):
    """警告（配色从配置）"""
    print(apply_style(msg, "warning"))


# ═══════════════════════════════════════════════════════════
# D. LLM 流 — 大模型的思考、工具调用、最终回复
#   注册名称: "llm_thought", "llm_tool", "llm_output"
#   支持按名称截断：llm_thought / llm_tool 可配置 truncation
# ═══════════════════════════════════════════════════════════

def llm_thought(msg: str):
    """LLM 思考过程（支持按配置截断）"""
    text = truncate(msg, "llm_thought")
    print(apply_style(text, "llm_thought"))


def llm_tool(msg: str):
    """工具调用 / 工具结果（支持按配置截断）"""
    text = truncate(msg, "llm_tool")
    print(apply_style(text, "llm_tool"))


def llm_output(text: str):
    """LLM 回复内容（流式，默认色，不换行）"""
    print(text, end="", flush=True)


def llm_newline():
    """LLM 回复结束后的换行"""
    print()


def llm_iteration(count: int):
    """打印迭代次数统计"""
    print(apply_style(f"📊 本次交互共迭代 {count} 次", "llm_iteration"))
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
        self._buffer = ""

    def think(self, text: str):
        """输出思考 token（配色从配置），首次自动显示思考标记"""
        if self.silent or not text:
            return
        if not self._thinking:
            # 如果之前输出过内容，先换行再显示思考标记
            prefix = "\n" if self._has_content else ""
            print(apply_style(f"{prefix}思考: ", "llm_thought"), end="", flush=True)
            self._thinking = True
        
        # 支持截断
        truncated = truncate(text, "llm_thought")
        print(apply_style(truncated, "llm_thought"), end="", flush=True)

    def content(self, text: str):
        """缓冲回复内容，等待 end() 时统一用 rich Markdown 渲染"""
        if self.silent or not text:
            return
        if self._thinking:
            print()  # 重置 + 换行
            self._thinking = False
        self._buffer += text
        self._has_content = True

    def end(self):
        """结束流式输出，用 rich 渲染完整的 Markdown 内容"""
        if self.silent:
            return
        if self._thinking:
            print(apply_style("", "llm_thought"))  # 重置并换行
        elif self._has_content and self._buffer:
            self._render_markdown(self._buffer)
            self._buffer = ""
        elif self._has_content:
            print()

    # ── 内部辅助 ──────────────────────────────────────

    @staticmethod
    def _render_markdown(text: str):
        """用 rich 渲染 Markdown，缺失时降级为纯文本"""
        try:
            from rich.markdown import Markdown
            from rich.console import Console
            Console().print(Markdown(text))
        except ImportError:
            print(text)


# ═══════════════════════════════════════════════════════════
# E. 系统日志 — 开发者调试用，默认隐藏
#   注册名称: "debug"
# ═══════════════════════════════════════════════════════════

def debug(msg: str):
    """调试日志，仅 DEBUG=1 时可见"""
    if os.environ.get("DEBUG"):
        print(apply_style(f"┐dbg│ {msg}", "debug"))


# ═══════════════════════════════════════════════════════════
# 🎨 仪式感 — 品牌记忆点
#   注册名称: "startup", "shutdown_panel", "logo"
# ═══════════════════════════════════════════════════════════

def startup(model: str):
    """启动横幅"""
    print(apply_style(f"🤖 Five Pebbels 已启动 (模型: {model})", "startup"))
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
    """打印 ASCII 启动画（支持按配置截断）"""
    art = truncate(LOGO_ART, "logo")
    print(apply_style(art, "logo"))
