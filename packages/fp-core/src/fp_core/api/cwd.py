"""会话工作目录（API 面）—— 前端在 turn 边界设置/复位。

实现见 `fp_core.core.cwd`；这里只做转出，避免前端越过唯一接口组
（协议 §5：前端只允许 import `fp_core.api` + 三个纯工具模块）。

用法（以 ACP 为例）::

    from fp_core.api.cwd import set_cwd, reset_cwd, cwd_scope

    token = set_cwd(session.cwd)      # turn 开始（同一 Task）
    try:
        await run_turn()
    finally:
        reset_cwd(token)             # turn 结束
"""

from fp_core.core.cwd import (
    current_cwd,
    cwd_bound,
    cwd_scope,
    reset_cwd,
    resolve_path,
    set_cwd,
)

__all__ = ["current_cwd", "cwd_bound", "cwd_scope", "reset_cwd", "resolve_path", "set_cwd"]
