"""Spawn 契约 —— 「一个实例被另一个实例拉起」这件事的**唯一**环境变量入口。

为什么单独成模块
================
这套变量是 **L3 前端 ←→ L4 资产之间的公开接口**。过去它散落在 fp_cli /
fp_core / 各个插件里，每处各写一遍 ``os.environ.get(...)``，于是「谁是子进程」
这件事有 N 份判据——改名必然漏一处，漏掉的那处就静默失效（例如某条自禁分支
永远不成立，而没人发现）。

收成一份后：

- **生产方**（任意前端）：:func:`declare_entry` 声明「怎么把我再拉起来一次」
- **消费方**（任意层，含 L4 资产）：:func:`depth` / :func:`is_spawned` /
  :func:`entry` / :func:`child_env`

为什么是**深度**而不是布尔
==========================
旧变量 ``FP_IS_SUBAGENT=1`` 有两个毛病：

1. **语义不足**：只能答「是不是」，答不了「第几层」。将来要允许两层派遣
   （深度 2）时，布尔变量没有表达能力，只能再加一个变量。
2. **命名耦合**：变量名把机制绑死在某个具体的派遣工具上。那个工具一旦搬出
   核心（变成可选资产）或改名，核心就得继续维护一个以它命名的变量——
   核心记住扩展的名字，是依赖方向反了。

``FP_SPAWN_DEPTH`` 是数值：``0``/未设 = 顶层实例，``>=1`` = 被拉起的子实例。
派遣工具叫什么都与它无关。

界面清单
========
=====================  ==============================================
``FP_SPAWN_DEPTH``     子实例深度（"0"/未设 = 顶层；"1" = 一层派遣）
``FP_SPAWN_SID``       父进程**预分配**的会话 ID（父进程据此兜底补写 meta）
``FP_SPAWN_PARENT_SID``父会话 ID（会话归属链）
``FP_SPAWN_SOURCE``    子会话来源标签（决定摘要前缀 / 列表 🔹 标记）
``FP_SPAWN_ENTRY``     前端自述的拉起命令（JSON argv 前缀）——依赖倒置的关键
``FP_QUIET``           不渲染 Logo/提示（headless 语义）
``FP_SILENT``          display.* 全静默（只留结果）
=====================  ==============================================

前四个由**派遣方**写、由**子实例自己**读；``FP_SPAWN_ENTRY`` 由**前端自己**
写、由**派遣方**读。方向相反正是要点：核心不知道前端是谁。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Any, cast

# ── 变量名（改这里 = 改全系统；消费方一律走下面的函数，不直接拼字符串）──
DEPTH_ENV = "FP_SPAWN_DEPTH"
SID_ENV = "FP_SPAWN_SID"
PARENT_SID_ENV = "FP_SPAWN_PARENT_SID"
SOURCE_ENV = "FP_SPAWN_SOURCE"
ENTRY_ENV = "FP_SPAWN_ENTRY"
QUIET_ENV = "FP_QUIET"
SILENT_ENV = "FP_SILENT"

MAX_DEPTH = 1
"""允许的最大派遣深度。

1 = 只允许「顶层 → 子」一层，子实例不可再派（等价于旧 ``FP_IS_SUBAGENT=1``
的递归守卫）。为什么保留这个上限：每多一层，链端到端的失败面就多一层
（超时、SIGKILL、会话收尾），而收益（并行再分解）尚未被证明。
放开时**只改这个常量**，别在各消费方写死。"""


def depth(env: Mapping[str, str] | None = None) -> int:
    """当前实例的派遣深度。未设/非法 = 0（顶层）。

    非法值按 0 处理而非报错：环境变量是跨进程接口，一个坏值不该让整个
    实例起不来——但它必须**可见**，否则「守卫静默失效」正是我们要避免的。
    深度解析是纯函数，告警留给调用方（见 :func:`is_spawned` 的注释）。
    """
    raw = (env if env is not None else os.environ).get(DEPTH_ENV)
    if not raw:
        return 0
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return 0


def is_spawned(env: Mapping[str, str] | None = None) -> bool:
    """当前实例是否被别的实例拉起（深度 >= 1）。"""
    return depth(env) >= 1


def can_spawn(env: Mapping[str, str] | None = None) -> bool:
    """当前实例还能否再拉起一层（深度 < :data:`MAX_DEPTH`）。"""
    return depth(env) < MAX_DEPTH


def env_value(name: str, env: Mapping[str, str] | None = None) -> str:
    """读一个 spawn 变量，缺失/空串返回 ``""``（统一成一种空值，免各处判断）。"""
    return str((env if env is not None else os.environ).get(name) or "")


def current_sid(env: Mapping[str, str] | None = None) -> str:
    """父进程预分配的会话 ID（无则 ``""``）。"""
    return env_value(SID_ENV, env)


def parent_sid(env: Mapping[str, str] | None = None) -> str:
    """父会话 ID（无则 ``""``）。"""
    return env_value(PARENT_SID_ENV, env)


def source(env: Mapping[str, str] | None = None) -> str:
    """子会话来源标签（无则 ``""``）。"""
    return env_value(SOURCE_ENV, env)


# ── 入口声明（依赖倒置）────────────────────────────────────────


def declare_entry(argv: Sequence[str], *, env: dict[str, str] | None = None) -> str:
    """**前端**调用：声明「把我再拉起一次」的 argv 前缀，写入 ``FP_SPAWN_ENTRY``。

    ``argv`` 到「提示词参数之前」为止，例如 ``[sys.executable, "-m", "fp_cli.main", "-m"]``。
    派遣方随后只需 ``entry() + [prompt]``。

    为什么由前端声明而不是核心写死：核心若写死 ``-m fp_cli.main``，就是核心
    反向依赖了某个具体前端（L2→L3 违规），而且换前端（ACP / WebUI / 第三方）
    派遣就静默失效。声明制让「谁能被拉起」变成运行期事实，而非编译期常量。
    """
    target = os.environ if env is None else env
    value = json.dumps([str(a) for a in argv], ensure_ascii=False)
    target[ENTRY_ENV] = value
    return value


def entry(env: Mapping[str, str] | None = None) -> list[str] | None:
    """派遣方调用：取前端声明的 argv 前缀。未声明/坏值 → ``None``。

    返回 ``None`` 而不是抛异常：调用方要能把「这个前端不支持被拉起」变成
    一句给 LLM 看得懂的拒绝语，而不是一个 traceback。
    """
    raw = env_value(ENTRY_ENV, env)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    argv = cast("list[Any]", parsed)
    if not all(isinstance(a, str) for a in argv):
        return None
    return [cast(str, a) for a in argv]


# ── 子实例环境构造 ──────────────────────────────────────


def child_env(
    base: Mapping[str, str] | None = None,
    *,
    sid: str = "",
    parent: str = "",
    source_name: str = "",
    silent: bool = False,
) -> dict[str, str]:
    """构造子实例的环境变量（继承父环境 + 写入派遣元数据）。

    集中在这里的好处：派遣方（L4 资产）不需要知道任何变量名，也无法
    「少设一个导致会话归属断链」——这类 bug 过去靠 review 发现，靠不住。
    """
    env = dict(base if base is not None else os.environ)
    env[DEPTH_ENV] = str(depth(base) + 1)
    if sid:
        env[SID_ENV] = sid
    if parent:
        env[PARENT_SID_ENV] = parent
    if source_name:
        env[SOURCE_ENV] = source_name
    if silent:
        env[SILENT_ENV] = "1"
    return env
