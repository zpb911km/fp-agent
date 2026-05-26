"""BoxPanel — 框线面板组件。

在终端中绘制类似这样的框线面板:

╔══════════════════════════════════════════╗
║  📂  会话结束                            ║
║  ──────────────────────────────────────  ║
║  总结: ...                               ║
╚══════════════════════════════════════════╝

支持标题、分隔线、多行内容、自定义宽度。
"""

from __future__ import annotations
from typing import Optional, Sequence


class BoxPanel:
    """框线面板 — 支持标题、分段、内容行。"""

    def __init__(self, width: int = 48):
        self.width = width

    def _sep(self, char: str = "─") -> str:
        return char * self.width

    def _line(self, text: str = "") -> str:
        return f"║  {text:<{self.width + 4}s}║"

    def top(self, title: str = "") -> str:
        """返回面板顶框。"""
        sep = self._sep()
        if title:
            return f"╔{sep}╗\n{self._line(title)}"
        return f"╔{sep}╗"

    def divider(self) -> str:
        """返回分隔线。"""
        return f"║{self._sep()}║"

    def body_line(self, text: str) -> str:
        """返回一条内容行。"""
        return self._line(text)

    def bottom(self) -> str:
        """返回面板底框。"""
        sep = self._sep()
        return f"╚{sep}╝"

    def render(self, title: str, lines: Sequence[str], sections: Sequence[Sequence[str]] | None = None) -> str:
        """完整渲染一个面板。

        Args:
            title: 面板标题（显示在顶框下方）
            lines: 直接显示在内容区的行（第一段）
            sections: 可选的分段列表，每个分段前自动插入分隔线

        Returns:
            拼接好换行符的完整面板字符串
        """
        parts = [self.top(title)]
        for line in lines:
            parts.append(self.body_line(line))

        if sections:
            for sec in sections:
                parts.append(self.divider())
                for line in sec:
                    parts.append(self.body_line(line))

        parts.append(self.bottom())
        return "\n".join(parts)

    @staticmethod
    def print_panel(panel_str: str) -> None:
        """直接打印面板。"""
        print(panel_str)
