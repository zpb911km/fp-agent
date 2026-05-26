"""StreamManager — 流式输出状态机。

管理 AI 回复内容的流式渲染，处理:
- 普通内容 (content) 与思考内容 (reasoning_content) 的切换
- ANSI 转义序列的开启/关闭
- 前缀标记
"""

from __future__ import annotations

import sys


class StreamManager:
    """流式输出管理器（有状态）。

    使用方式:
        sm = StreamManager()
        sm.begin()
        sm.on_reasoning("第一步思考...")
        sm.on_content("这是回答。")
        sm.end()
    """

    def __init__(self) -> None:
        self._reasoning_shown = False

    @property
    def is_reasoning_active(self) -> bool:
        return self._reasoning_shown

    def begin(self) -> None:
        """开始流式输出，打印前缀。"""
        self._reasoning_shown = False
        print("AI -> ", end="", flush=True)

    def on_reasoning(self, text: str) -> None:
        """输出一段思考内容。"""
        if not self._reasoning_shown:
            print("\033[2m[思考]", end="", flush=True)
            self._reasoning_shown = True
        print(text, end="", flush=True)

    def on_content(self, text: str) -> None:
        """输出一段普通内容（如果正在思考模式，先关闭思考模式）。"""
        if self._reasoning_shown:
            print("\033[0m]\n", end="", flush=True)
            self._reasoning_shown = False
        print(text, end="", flush=True)

    def end(self) -> None:
        """结束流式输出。"""
        if self._reasoning_shown:
            print("\033[0m]", flush=True)
            self._reasoning_shown = False
        else:
            print(flush=True)

    def reset(self) -> None:
        """重置状态（不输出任何内容）。"""
        self._reasoning_shown = False
