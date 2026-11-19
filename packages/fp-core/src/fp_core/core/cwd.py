"""会话工作目录 —— 用 contextvar 承载，而不是 `os.chdir`。

为什么不能用 `os.chdir`：那是**进程级**全局可变状态，而「会话」是协议级概念
（ACP 一条连接可并存多条会话，各带自己的 cwd）。把 B 的目录塞进进程全局变量，
正确性只能靠「同一时刻只有一个会话在跑」这条**纪律**兜着 —— 没有语言机制保障。
任何一处新代码（后台 job、subagent、插件里 await 之后顺手 chdir）都会把正在
跑的会话带到别的项目里：没有异常、没有日志、没有测试会红，事后才发现某个提交
悄悄改坏了别人的仓库。

`ContextVar` 是**按 Task 隔离**的：每个 turn 是一个 Task，它里面 `await` 出来的
代码读到的都是自己会话的 cwd，即使将来放开并发也不会互相污染。于是正确性从
「纪律」升级为「机制」。

未设置时回落 `os.getcwd()`：终端/单会话前端不需要设置，行为与改造前完全一致。
"""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token

__all__ = ["current_cwd", "cwd_bound", "cwd_scope", "reset_cwd", "resolve_path", "set_cwd"]

_current: ContextVar[str | None] = ContextVar("fp_session_cwd", default=None)


def current_cwd() -> str:
    """当前执行上下文的工作目录；未设置时回落进程 cwd。

    「未设置」不是错误：终端、单会话前端、测试都不设置它，语义就是
    「进程当前目录」，与 os.getcwd() 等价。
    """
    value = _current.get()
    return value if value else os.getcwd()


def resolve_path(path: str) -> str:
    """把（可能相对的）路径按**当前会话**的 cwd 解析。

    绝对路径原样返回（不 realpath：保留用户写法，也避免把软链解析成实体路径
    让回显的路径与用户输入对不上）。
    """
    if not path:
        return path
    return path if os.path.isabs(path) else os.path.join(current_cwd(), path)


def set_cwd(path: str) -> Token[str | None]:
    """设置当前上下文的会话 cwd，返回复位令牌（必须在同一 Task 内 reset）。"""
    return _current.set(os.path.abspath(path))


def cwd_bound() -> bool:
    """本上下文是否**已经显式**指定了 cwd（区别于「回落进程目录」）。

    用途单一：turn 边界的兜底绑定要判断「该不该出手」。
      - WebUI 每个 HTTP/WS 请求是一个新 Task，切换会话时设的值传不过来
        （contextvar 按 Task 隔离）→ 这里是 False → 由 portal 按会话根补上；
      - ACP 在 turn Task 里显式设过（客户端声明的 cwd 优先）→ True → 不覆盖；
      - CLI 在主线程上下文设过 → 顺着 Turn 创建继承下来 → True。
    若把「回落进程目录」也当成已设置，兜底就永远不生效，WebUI 又会静默落在
    启动目录里。
    """
    return _current.get() is not None


def reset_cwd(token: Token[str | None]) -> None:
    """复位到 set_cwd 之前的值。"""
    _current.reset(token)


@contextmanager
def cwd_scope(path: str) -> Generator[str, None, None]:
    """with 块内工作目录为 `path`（异常安全）。

    适用同步/单轮场景；turn 循环（需要精确控制作用域）请直接用
    set_cwd/reset_cwd。
    """
    token = set_cwd(path)
    try:
        yield current_cwd()
    finally:
        reset_cwd(token)
