"""
可复用 UI 组件

提供一些高级组件，减少 console.py 中的重复代码。
所有组件均基于 interface.py 中的底层方法构建。
"""

from .tokens import Style, Fg, Border, strip_ansi


def panel(title: str, content: str, width: int = 50) -> str:
    """绘制带标题的盒子面板
    
    Args:
        title: 面板标题（含图标）
        content: 面板正文（多行文本）
        width: 面板宽度（不含边框）
    
    Returns:
        格式化的面板字符串
    """
    sep = Border.H * width
    lines = content.split("\n")
    
    buf = [f"\n{Border.TL}{sep}{Border.TR}"]
    if title:
        buf.append(f"{Border.V}  {title:<{width+2}s}{Border.V}")
        buf.append(f"{Border.V}{'─' * (width + 4)}{Border.V}")  # inner separator
    
    for line in lines:
        # 计算可见宽度（移除了ANSI转义码后的宽度）
        visible = strip_ansi(line)
        padding = width + 4 - len(visible)  # +4 for "  " and "  "
        if padding < 0:
            padding = 0
        buf.append(f"{Border.V}  {line}{' ' * padding}{Border.V}")
    
    buf.append(f"{Border.BL}{sep}{Border.BR}")
    return "\n".join(buf)


def stats_table(items: list[tuple[str, str]], width: int = 50) -> str:
    """绘制键值对统计表
    
    Args:
        items: (label, value) 列表
        width: 表格宽度
    
    Returns:
        格式化的表格字符串
    """
    sep = Border.H * width
    lines = []
    for label, value in items:
        line = f"{label}: {value}"
        visible_len = len(strip_ansi(line))
        padding = (width + 2) - visible_len
        if padding < 0:
            padding = 0
        lines.append(f"{Border.V}  {line}{' ' * padding}{Border.V}")
    return f"{Border.TL}{sep}{Border.TR}\n" + \
           "\n".join(lines) + \
           f"\n{Border.BL}{sep}{Border.BR}"


def progress_bar(current: int, total: int, width: int = 20) -> str:
    """绘制进度条
    
    Args:
        current: 当前值
        total: 总值
        width: 字符宽度
    
    Returns:
        进度条字符串，如 [████░░░░░░] 50%
    """
    if total <= 0:
        return ""
    filled = int(current / total * width)
    bar = "█" * filled + "░" * (width - filled)
    pct = int(current / total * 100)
    return f"[{bar}] {pct}%"


def badge(text: str, fg_color: str = "") -> str:
    """创建带样式的标签"""
    return f"{fg_color}{Style.BOLD}[{text}]{Style.RESET}"


def section(title: str) -> str:
    """创建段落标题分隔线"""
    return f"\n{Style.BOLD}── {title} ──{Style.RESET}\n"


def indent(text: str, prefix: str = "  ") -> str:
    """为多行文本添加缩进"""
    return "\n".join(prefix + line for line in text.split("\n"))
