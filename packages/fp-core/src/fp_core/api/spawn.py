"""spawn 契约的前端面（唯一接口组的 re-export，不新增逻辑）。

为什么需要这一层
================
``fp_core.spawn`` 是「一个实例被另一个实例拉起」这件事的唯一契约模块，但
前端（terminal / webui / acp / 路由包）按协议 §5 **只许导入** ``fp_core.api``。
于是契约必须经本模块转出一次——否则前端要么越界导入（审计红），要么各自
手抄一份 ``os.environ.get("FP_SPAWN_*")``（那正是这个模块要消灭的东西）。

它是纯转出：没有任何编译期常量或分支，实现全在 ``fp_core.spawn``。
核心侧（含 L4 资产）直接导入 ``fp_core.spawn`` 即可，不必绕行。

用法::

    from fp_core.api import spawn

    spawn.declare_entry([sys.executable, "-m", "fp.main", "--message"])  # 前端：声明能被拉起
    if spawn.is_spawned(): ...                                          # 消费方：我是被拉起的
"""

from fp_core.spawn import (
    DEPTH_ENV,
    ENTRY_ENV,
    MAX_DEPTH,
    PARENT_SID_ENV,
    QUIET_ENV,
    SID_ENV,
    SILENT_ENV,
    SOURCE_ENV,
    can_spawn,
    child_env,
    current_sid,
    declare_entry,
    depth,
    entry,
    env_value,
    is_spawned,
    parent_sid,
    source,
)

__all__ = [
    "DEPTH_ENV",
    "ENTRY_ENV",
    "MAX_DEPTH",
    "PARENT_SID_ENV",
    "QUIET_ENV",
    "SID_ENV",
    "SILENT_ENV",
    "SOURCE_ENV",
    "can_spawn",
    "child_env",
    "current_sid",
    "declare_entry",
    "depth",
    "entry",
    "env_value",
    "is_spawned",
    "parent_sid",
    "source",
]
