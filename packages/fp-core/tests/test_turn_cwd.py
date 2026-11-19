"""turn 边界的 cwd 兜底（`Portal._turn_cwd`）—— WebUI 不静默落错目录。

为什么单独立一层：`core/cwd.py` 只证明 contextvar 能存能取、`session.py` 只证明
**装载**会话时会绑定；而 WebUI 的真实执行路径是「`/switch` 一个请求 Task，下一条
消息是另一个请求 Task」。ContextVar 按 Task 隔离，`set_cwd` 传不过去——于是
切换会话后一切提示正常，工具却仍在**进程启动目录**里读写。这里断言的就是那个
跨 Task 的补丁点。

四个断言对应四种必须区分的状态：
1. 上下文**没**指定 → 用当前会话根（这是兜底存在的理由）；
2. 上下文**已**指定（ACP 客户端在 session/load 声明的 cwd）→ 绝不覆盖；
3. 会话根**不存在**（项目被删/改名）→ 不动基准，别把每个工具都推去撞 ENOENT；
4. 退出 with 块要**复位**——否则一次兜底会污染同 Task 里后续的代码。
"""

from __future__ import annotations

import contextvars
import os
from types import SimpleNamespace

import pytest

from fp_core.api.portal import Portal
from fp_core.core.cwd import current_cwd, cwd_bound, reset_cwd, set_cwd


class _StubPortal:
    """只提供 `_turn_cwd` 用到的两个成员（`_require().state.session`）。

    直接调 `Portal._turn_cwd(stub)`：被测的是**生产代码本身**，不是副本。
    真 Portal 需要 open/agent/lifecycle 全套，为了测这 8 行不值得，也容易把
    测试绑死在 bootstrap 细节上。
    """

    def __init__(self, root: str | None) -> None:
        self.state = SimpleNamespace(session=SimpleNamespace(session_root=lambda: root))

    def _require(self) -> _StubPortal:
        return self


def _in_fresh_context(fn):
    """在**空白 Context**里跑（contextvar 全为默认值）。

    不能依赖「pytest 进程里刚好没人设过」：同进程其它用例 `set_cwd` 之后若
    忘了复位，这里的 `cwd_bound()` 就会是 True，兜底被跳过 —— 测试假绿。
    """
    return contextvars.Context().run(fn)


@pytest.fixture
def roots(tmp_path):
    exist = tmp_path / "projA"
    exist.mkdir()
    return str(exist), str(tmp_path / "gone")  # 后者故意不创建


def test_binds_session_root_when_context_has_none(roots):
    exist, _ = roots

    def body():
        assert cwd_bound() is False, "前提：空白上下文里没有指定 cwd"
        with Portal._turn_cwd(_StubPortal(exist)):  # pyright: ignore[reportArgumentType]
            assert current_cwd() == os.path.abspath(exist)

    _in_fresh_context(body)


def test_does_not_override_explicitly_bound_cwd(roots, tmp_path):
    """ACP 的客户端声明优先：session/load 传的 cwd 覆盖落盘 meta 的根。"""
    exist, _ = roots
    client = tmp_path / "clientSaid"
    client.mkdir()

    def body():
        token = set_cwd(str(client))
        try:
            with Portal._turn_cwd(_StubPortal(exist)):  # pyright: ignore[reportArgumentType]
                assert current_cwd() == os.path.abspath(str(client)), "显式指定不该被会话根顶掉"
        finally:
            reset_cwd(token)

    _in_fresh_context(body)


def test_skips_missing_directory(roots):
    exist, gone = roots

    def body():
        with Portal._turn_cwd(_StubPortal(gone)):  # pyright: ignore[reportArgumentType]
            assert current_cwd() == os.getcwd(), "目录不存在 → 保持进程基准，别让工具全撞 ENOENT"
            assert cwd_bound() is False, "没设就是没设，不能假装设过"
        # 会话连根都没记（老会话）→ 同样不动
        with Portal._turn_cwd(_StubPortal(None)):  # pyright: ignore[reportArgumentType]
            assert current_cwd() == os.getcwd()

    _in_fresh_context(body)


def test_restores_on_exit(roots):
    """兜底是一次性作用域：退出后必须回到「未指定」，否则会污染同 Task。"""
    exist, _ = roots

    def body():
        with Portal._turn_cwd(_StubPortal(exist)):  # pyright: ignore[reportArgumentType]
            assert cwd_bound() is True
        assert cwd_bound() is False
        assert current_cwd() == os.getcwd()

    _in_fresh_context(body)


def test_restores_on_exception(roots):
    exist, _ = roots

    def body():
        with (  # noqa: SIM117 —— 故意嵌套：断言的是 with 体内部抛异常的路径
            pytest.raises(RuntimeError),
            Portal._turn_cwd(_StubPortal(exist)),  # pyright: ignore[reportArgumentType]
        ):
            raise RuntimeError("转轮内炸了")
        assert cwd_bound() is False, "异常路径也要复位（否则后面整轮都在别人的仓库里跑）"

    _in_fresh_context(body)
