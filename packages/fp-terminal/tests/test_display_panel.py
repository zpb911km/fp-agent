# pyright: reportPrivateUsage=false
"""退出总结面板（display.shutdown_panel）的宽度自适应回归测试。

背景：面板宽度上限原为硬编码 60（内容区 54），一旦内容行超宽就被 `…` 截断，
信息静默丢失——真实案例：Token 行的 `×546)` 被截成 `×54…`。
现在要求：面板跟随内容/终端宽度自适应，任何长度都不丢字符（超宽折行）。
"""

import contextlib
import io
import os
import unittest
from unittest.mock import patch

from fp_cli.display import _display_width, _wrap_by_width, shutdown_panel
from fp_core.api import TokenUsage


def _render(
    summary: str,
    file: str,
    model: str,
    msg_count: int,
    created: str,
    duration: str = "",
    token_usage: TokenUsage | None = None,
) -> list[str]:
    """调用 shutdown_panel 并捕获输出行"""
    buf = io.StringIO()
    env = {k: v for k, v in os.environ.items() if k != "FP_SUBAGENT_SILENT"}
    with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(buf):
        shutdown_panel(
            summary=summary,
            file=file,
            model=model,
            msg_count=msg_count,
            created=created,
            duration=duration,
            token_usage=token_usage,
        )
    return buf.getvalue().splitlines()


def _long_token_usage() -> TokenUsage:
    """真实场景：session s_261011_153007051527（Token 行显示宽 55 > 旧上限 54）"""
    return TokenUsage(
        prompt_tokens=50668721,
        completion_tokens=447181,
        total_tokens=51115902,
        call_count=546,
        cache_hit_tokens=49792256,
        cache_miss_tokens=876465,
        reasoning_tokens=289047,
    )


class TestWrapByWidth(unittest.TestCase):
    def test_no_char_lost(self):
        text = "这是一段很长的中文总结" * 7
        segs = _wrap_by_width(text, 30)
        self.assertEqual("".join(segs), text)
        self.assertTrue(all(_display_width(s) <= 30 for s in segs))

    def test_prefers_space_break(self):
        text = "Token: 51,115,902 (↑50,668,721 ↓447,181 cache:98% ×546)"
        segs = _wrap_by_width(text, 54)
        # 断行处的空格被换行取代（word-wrap 正常行为），其余字符零丢失
        self.assertEqual("".join(segs).replace(" ", ""), text.replace(" ", ""))
        self.assertTrue(all(_display_width(s) <= 54 for s in segs))
        # 数字/词不被切碎：带 ×546) 的片段应完整落在某一行
        self.assertTrue(any(s.strip() == "×546)" for s in segs), segs)

    def test_explicit_newline_splits_paragraphs(self):
        segs = _wrap_by_width("第一行\n第二行", 40)
        self.assertEqual(segs, ["第一行", "第二行"])

    def test_wide_char_not_lost(self):
        segs = _wrap_by_width("📂📂📂📂", 2)
        self.assertEqual("".join(segs), "📂📂📂📂")
        self.assertTrue(all(_display_width(s) <= 2 for s in segs))


class TestShutdownPanel(unittest.TestCase):
    def test_token_line_not_truncated(self):
        lines = _render(
            summary="算了.得不偿失.收工",
            file="s_261011_153007051527.jsonl",
            model="deepseek-flash",
            msg_count=72,
            created="2026-10-11 15:30:07",
            duration="7:21:45",
            token_usage=_long_token_usage(),
        )
        joined = "\n".join(lines)
        self.assertNotIn("…", joined)  # 不得静默截断
        self.assertIn("×546)", joined)  # 完整保留调用次数与右括号

    def test_frame_aligned(self):
        lines = _render(
            summary="短",
            file="s_1.jsonl",
            model="m",
            msg_count=1,
            created="2026-01-01 00:00:00",
            token_usage=_long_token_usage(),
        )
        widths = {_display_width(line) for line in lines if line}
        self.assertEqual(len(widths), 1, f"框线未对齐: {sorted(widths)}")

    def test_fits_narrow_terminal(self):
        with patch.dict(os.environ, {"COLUMNS": "60"}):
            lines = _render(
                summary="超长总结" * 20,
                file="s_1.jsonl",
                model="deepseek-flash",
                msg_count=72,
                created="2026-01-01 00:00:00",
                token_usage=_long_token_usage(),
            )
        self.assertTrue(all(_display_width(line) <= 58 for line in lines), lines)
        widths = {_display_width(line) for line in lines if line}
        self.assertEqual(len(widths), 1)

    def test_long_content_wraps_within_frame(self):
        summary = "很长的中文总结" * 20
        lines = _render(
            summary=summary,
            file="s_1.jsonl",
            model="m",
            msg_count=1,
            created="2026-01-01 00:00:00",
        )
        framed = [line for line in lines if line]
        for line in framed:
            if line.startswith("║"):
                self.assertTrue(line.endswith("║"), line)
            else:  # 上下边界
                self.assertTrue(line[0] in "╔╚" and line[-1] in "╗╝", line)
        # 折行后字符无丢失（去掉框线与补白）
        body = "".join(line[2:-2] for line in framed if line.startswith("║"))
        for ch in summary:
            self.assertIn(ch, body)


if __name__ == "__main__":
    unittest.main()
