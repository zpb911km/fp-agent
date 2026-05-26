"""display/components/ — 可复用的 TUI 渲染组件。

每个组件是独立、无状态的渲染函数或轻量类，
可被不同 Display 实现组合使用。
"""

from display.components.box_panel import BoxPanel
from display.components.streaming import StreamManager

__all__ = ["BoxPanel", "StreamManager"]
