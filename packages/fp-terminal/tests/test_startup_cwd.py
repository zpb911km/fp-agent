# pyright: reportPrivateUsage=false
"""启动面板 / 切换提示里的「工作目录」行（cwd 是会话属性的可见性契约）

背景：把 cwd 从「进程级 `os.chdir`」改成「会话属性」后，出现一条新的静默路径：
`fp --resume <跨项目会话>`（或 reload 续接）时进程在 B 启动、会话根是 A，
用户的相对路径语义已经换了，而屏幕上没有任何变化 —— 文件写进另一个仓库，
不报错、不写日志。这两处提示就是这条语义变更的全部可见性，因此逐条断言。

只用纯函数，不启动 TUI：提示是「字符串里有/没有」的问题。
"""

import os
from unittest.mock import patch

from fp_cli.display import _build_startup_frame, _startup_dir_line
from fp_core.api.cwd import current_cwd, reset_cwd, set_cwd


def _rows(model: str = "m", resume: bool = True) -> list[str]:
    lines, _ = _build_startup_frame(model, resume)
    return lines


class TestStartupDirLine:
    def test_same_dir_no_line(self, tmp_path):
        """会话根 == 启动目录 → 一行都不加（默认场景零噪声）"""
        tok = set_cwd(str(tmp_path))
        try:
            with patch("os.getcwd", return_value=str(tmp_path)):
                assert _startup_dir_line() == ""
                assert not any("Dir:" in r for r in _rows())
        finally:
            reset_cwd(tok)

    def test_cross_project_session_shows_dir(self, tmp_path):
        """会话根 ≠ 启动目录 → 面板多一行 `Dir:  <会话根>`（这是唯一的告知通道）"""
        launch = tmp_path / "B"
        root = tmp_path / "A"
        launch.mkdir()
        root.mkdir()
        tok = set_cwd(str(root))
        try:
            with patch("os.getcwd", return_value=str(launch)):
                rows = _rows()
                dir_rows = [r for r in rows if "Dir:" in r]
                assert len(dir_rows) == 1, rows
                # 取出用户真正看到的那段：`  |<居中>Dir:  <路径><居中>|` → `<路径>`
                shown = dir_rows[0][3:-1].strip().split("Dir:", 1)[1].strip()
                # pytest 的 tmp_path 本身就超 46 列内容区 → 截成 `…/尾部`，
                # 所以判据是「尾部必须是会话根的真实后缀」，而非整串出现
                tail = shown[1:] if shown.startswith("…") else shown
                assert str(root).endswith(tail), (shown, str(root))
                # 面板宽度没被撑破（内容行居中填充进 |...|）
                assert all(len(r) == len(rows[0]) for r in rows), [len(r) for r in rows]
        finally:
            reset_cwd(tok)

    def test_long_path_is_shortened_not_overflowing(self, tmp_path):
        """超长路径要截成 `…/尾部`：内容区固定 46 列，溢出会把面板撑歪"""
        deep = tmp_path / ("x" * 60) / ("y" * 40)
        deep.mkdir(parents=True)
        tok = set_cwd(str(deep))
        try:
            with patch("os.getcwd", return_value=str(tmp_path)):
                rows = _rows()
                widths = {len(r) for r in rows}
                assert len(widths) == 1, rows  # 所有行等宽
                assert any("Dir:" in r and "…" in r for r in rows), rows
        finally:
            reset_cwd(tok)

    def test_no_bound_cwd_falls_back_to_process_dir(self, tmp_path):
        """未设置会话根（老前端 / 首启）→ 回落进程目录，与改造前完全一致

        「未设置」是 ContextVar 的**默认值**，不是 `set_cwd(进程目录)`——后者是
        显式绑定，进程目录一变就会显示成「跨项目」（这恰是本测试上一版写错的地方：
        用 set_cwd 模拟未设置，等于自己制造了绑定）。故在全新 Context 里跑。
        """
        import contextvars

        def scenario() -> None:
            with patch("os.getcwd", return_value=str(tmp_path)):
                assert _startup_dir_line() == ""
                assert os.path.abspath(current_cwd()) == os.path.abspath(str(tmp_path))

        contextvars.Context().run(scenario)  # 空上下文：_current 从未被 set
