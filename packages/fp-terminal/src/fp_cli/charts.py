"""终端图表原语 — 零依赖（不引 plotext，依赖最小化硬约束）

数据 → 文本渲染：unicode 条形图（行式）+ textual Sparkline（波形）。
供 TUI 统计面板（StatsModal）与未来图表组件复用。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def bar_rows(items: Mapping[str, int], *, width: int = 24, unit: str = "") -> list[str]:
    """横向条形图行：`label  ████ 123 unit`。

    Args:
        items: {标签: 数值}，按插入序渲染（调用方决定排序）
        width: 条形最大字符宽度
        unit: 数值后缀单位
    Returns:
        等宽文本行列表（标签左对齐补齐，保证条形起始列对齐）
    """
    if not items:
        return ["（无数据）"]
    label_w = max(len(k) for k in items)
    peak = max(items.values()) or 1
    rows: list[str] = []
    for label, value in items.items():
        filled = round(value / peak * width) if peak else 0
        bar = "█" * filled + "·" * (width - filled)
        rows.append(f"{label.ljust(label_w)}  {bar} {value}{unit}")
    return rows


def spark_segments(data: Sequence[float], *, segments: str = "▁▂▃▄▅▆▇█") -> str:
    """把数值序列渲染成一行 sparkline 文本（不依赖 textual，纯字符串可测）。

    空序列 → 空串；全零 → 用最低段占位。
    """
    if not data:
        return ""
    peak = max(data) or 1.0
    n = len(segments)
    return "".join(segments[min(n - 1, int(v / peak * (n - 1)))] for v in data)
