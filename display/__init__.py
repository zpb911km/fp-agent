"""
TUI 模块 — 显示层与逻辑层分离

职责：
- 将 agent.py 中所有 print() / ANSI 转义码输出集中于此
- 提供统一的 Display 抽象接口
- 支持多种后端实现（Console, curses, GUI）

快速开始：
    from display import get_display
    display = get_display()
    display.on_startup("gpt-4")
    display.on_ai_response_chunk("Hello, world!")
"""

from .interfaces import Display
from .console import ConsoleDisplay, render_markdown


# ── 全局单例 ────────────────────────────────────

_display: Display | None = None


def get_display(backend: str = "console", **kwargs) -> Display:
    """获取 Display 实例（工厂方法）
    
    Args:
        backend: 后端类型（当前仅支持 'console'）
        **kwargs: 传递给 Display 实现的参数
    
    Returns:
        Display 实现实例
    
    后续可扩展:
        backend='curses' → CursesDisplay
        backend='gui'    → GtkDisplay / QtDisplay
    """
    global _display
    if _display is not None:
        return _display

    if backend == "console":
        _display = ConsoleDisplay(**kwargs)
    else:
        raise ValueError(f"不支持的显示后端: {backend}. 可用: console")

    return _display


def reset_display() -> None:
    """重置全局 Display 实例（主要用于测试）"""
    global _display
    _display = None


__all__ = [
    "Display",
    "ConsoleDisplay",
    "get_display",
    "reset_display",
    "render_markdown",
]
