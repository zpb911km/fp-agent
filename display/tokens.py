"""
样式 Token 常量

集中管理所有 ANSI 颜色代码、图标符号和格式化常量。
便于统一修改主题，也方便未来扩展到 256 色/TrueColor。
"""

# ── ANSI 转义序列 ──────────────────────────────

class Style:
    """文本样式"""
    RESET      = "\033[0m"
    BOLD       = "\033[1m"
    DIM        = "\033[2m"
    ITALIC     = "\033[3m"
    UNDERLINE  = "\033[4m"
    REVERSE    = "\033[7m"

class Fg:
    """前景色"""
    BLACK   = "\033[30m"
    RED     = "\033[31m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN    = "\033[36m"
    WHITE   = "\033[37m"
    GRAY    = "\033[90m"       # 亮黑 = 灰色

class Bg:
    """背景色"""
    BLACK   = "\033[40m"
    RED     = "\033[41m"
    GREEN   = "\033[42m"
    YELLOW  = "\033[43m"
    BLUE    = "\033[44m"
    MAGENTA = "\033[45m"
    CYAN    = "\033[46m"
    WHITE   = "\033[47m"


# ── Markdown 渲染样式映射 ───────────────────────

MD_STYLES = {
    "code_block":   Fg.CYAN,              # ```code``` 
    "inline_code":  Fg.CYAN,              # `code`
    "bold":         Style.BOLD,           # **text**
    "italic":       Style.DIM,            # *text*
    "list_marker":  Fg.YELLOW,            # -, >, >
    "quote_marker": Fg.YELLOW,            # > text
}

# ── 图标符号（可切换为纯 ASCII）────────────────

class Icons:
    """UI 图标集合
    
    设置 Icons.mode = 'ascii' 可在不支持 UTF-8 的终端回退。
    """
    mode = "unicode"  # 'unicode' | 'ascii'

    # 流式 AI 输出
    AI_PREFIX       = property(lambda _: "AI -> " if Icons.mode == "ascii" else "🤖  ")
    THINKING_BADGE  = property(lambda _: "[thinking]" if Icons.mode == "ascii" else "\033[2m[思考]\033[0m")

    # 工具调用
    TOOL_CALL       = property(lambda _: "  Tool: " if Icons.mode == "ascii" else "  🛠️  ")
    TOOL_RESULT     = property(lambda _: "  => " if Icons.mode == "ascii" else "  📋  ")

    # 状态指示
    INFO            = property(lambda _: "[i] " if Icons.mode == "ascii" else "ℹ️  ")
    WARNING         = property(lambda _: "[!] " if Icons.mode == "ascii" else "⚠️  ")
    ERROR           = property(lambda _: "[x] " if Icons.mode == "ascii" else "❌  ")
    SUCCESS         = property(lambda _: "[v] " if Icons.mode == "ascii" else "✅  ")
    STATS           = property(lambda _: "  Stats: " if Icons.mode == "ascii" else "📊  ")

    # 加载
    LOADING         = property(lambda _: "  loading: " if Icons.mode == "ascii" else "🔄  ")
    SAVED           = property(lambda _: "  saved: " if Icons.mode == "ascii" else "💾  ")
    TASK            = property(lambda _: "  task: " if Icons.mode == "ascii" else "📂  ")

    # 自动推进
    AUTO_START      = property(lambda _: "  [auto] " if Icons.mode == "ascii" else "🔄 [自动] ")
    AUTO_PAUSED     = property(lambda _: "  [pause] " if Icons.mode == "ascii" else "⏸️  ")
    AUTO_ERROR      = property(lambda _: "  [stop] " if Icons.mode == "ascii" else "⏹️  ")

    # 会话
    SESSION_START   = property(lambda _: "=== " if Icons.mode == "ascii" else "📂  ")
    SESSION_END     = property(lambda _: "=== " if Icons.mode == "ascii" else "👋  ")

    # 清理
    CLEARED         = property(lambda _: "  cleared: " if Icons.mode == "ascii" else "🧹  ")

    # 输入提示符
    PROMPT          = property(lambda _: "> " if Icons.mode == "ascii" else "❯ ")

    # 分隔线
    SEPARATOR       = property(lambda _: "-" * 48 if Icons.mode == "ascii" else "─" * 48)


icons = Icons()  # 单例


# ── 边框样式 ────────────────────────────────────

class Border:
    """盒子绘制字符"""
    H      = "─"
    V      = "║"
    TL     = "╔"
    TR     = "╗"
    BL     = "╚"
    BR     = "╝"

    @classmethod
    def ascii(cls):
        """切换到纯 ASCII 模式"""
        cls.H = "-"
        cls.V = "|"
        cls.TL = "+"
        cls.TR = "+"
        cls.BL = "+"
        cls.BR = "+"


# ── 辅助函数 ─────────────────────────────────────

def styled(text: str, *styles: str) -> str:
    """用多个样式包裹文本"""
    prefix = "".join(styles)
    suffix = Style.RESET if styles else ""
    return f"{prefix}{text}{suffix}"


def strip_ansi(text: str) -> str:
    """移除所有 ANSI 转义序列"""
    import re
    return re.sub(r"\033\[[0-9;]*[a-zA-Z]", "", text)
