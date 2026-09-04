"""
commands/__init__.py — 命令注册表与自动发现

自动扫描 commands/ 目录下所有 .py 文件（排除 __init__.py），
导入每个模块并检查 name/execute 接口，构建命令名→模块的映射（含别名）。

命令接口：
    所有命令的 execute 签名统一为:
      async def execute(state: State, arg: str) -> tuple[bool, str]

    其中 State 是 fp_core.core.state.State 实例，提供命令所需的
    全部核心状态访问（conversation / session / llm / lifecycle 等）。
"""

import importlib
import importlib.util
import inspect
import os
from types import ModuleType
from typing import Any, cast

from fp_core.logger import get_logger

# 缓存：命令名 → 模块对象
_commands: dict[str, ModuleType] = {}

# 动态注册登记簿：仅记录 register_command() 注册过的主名。
# 用途：unregister 只允许撤销动态命令，防止误删自动发现的文件命令
# （文件命令以文件存在为生命周期，不参与动态清理）。
_dynamic_names: set[str] = set()


# 类型标注
class CommandModule:
    name: str
    aliases: list[str]
    description: str

    # execute 返回 (已处理, 输出文本)；
    # 兼容旧版：也可只返回 bool（自动转为 ("", False/True)）
    # 同步或异步均可，由 execute() 自动适配
    async def execute(self, state: object, arg: str) -> tuple[bool, str]:
        return (False, "")


def _discover_commands():
    """扫描并注册所有命令模块（内置 → 三来源，同名覆盖 + 警告）"""
    global _commands, _dynamic_names
    _commands = {}
    _dynamic_names = set()

    builtin_dir = os.path.dirname(os.path.abspath(__file__))
    _scan_dir(builtin_dir, "fp_core.commands")

    # 三来源用户命令目录（fetched → public → private，后加载覆盖先加载 + 警告）
    # 优先级：private > public > fetched（_scan_dir 内已有重复覆盖警告）
    from fp_core.config import user_dirs

    for user_dir in user_dirs("commands"):
        _scan_dir(user_dir)  # 直接 import 路径，通过 sys.path 解析


def _scan_dir(directory: str, package_prefix: str | None = None):
    """扫描单个目录下的命令文件"""
    if not os.path.isdir(directory):
        return

    for fname in sorted(os.listdir(directory)):
        if not fname.endswith(".py") or fname == "__init__.py":
            continue

        mod_name = fname[:-3]
        try:
            if package_prefix:
                mod = importlib.import_module(f"{package_prefix}.{mod_name}")
            else:
                spec = importlib.util.spec_from_file_location(mod_name, os.path.join(directory, fname))
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
        except Exception as e:
            get_logger().warning(f"⚠️  命令加载失败 [{mod_name}]: {e}")
            continue

        # 校验接口
        if not hasattr(mod, "name") or not hasattr(mod, "execute"):
            get_logger().warning(f"⚠️  命令模块 [{mod_name}] 缺少 name/execute，已跳过")
            continue

        name = mod.name
        if name in _commands:
            get_logger().warning(f"⚠️  命令 [{name}] 重复定义，已覆盖")
        _commands[name] = mod

        # 注册别名
        for alias in getattr(mod, "aliases", []):
            if alias in _commands:
                get_logger().warning(f"⚠️  别名 [{alias}] 与已有命令/别名冲突，已跳过")
                continue
            _commands[alias] = mod


_discover_commands()


def get_command(name: str) -> ModuleType | None:
    """根据命令名（含斜杠）或别名查找命令模块"""
    return _commands.get(name)


def get_all_commands() -> dict[str, str]:
    """返回 {命令名: 描述} 字典（只返回主名称，不含别名）"""
    result: dict[str, str] = {}
    seen: set[int] = set()
    for name, mod in _commands.items():
        mod_id = id(mod)
        if mod_id in seen:
            continue
        if hasattr(mod, "name") and mod.name == name:
            result[name] = getattr(mod, "description", "")
            seen.add(mod_id)
    return result


async def execute(state: object, cmd_name: str, arg: str) -> tuple[bool, str]:
    """执行命令，返回 (是否已处理, 输出文本)。

    命令的 execute 接收 State 参数（而非 Agent），直接操作核心状态。
    兼容旧版只返回 bool 的命令（自动补为 ("", False/True)）。
    """
    mod = get_command(cmd_name)
    if mod is None:
        return (False, "")

    # 执行命令（自动适配同步/异步）
    result: Any
    if inspect.iscoroutinefunction(mod.execute):
        result = await mod.execute(state, arg)
    else:
        result = mod.execute(state, arg)

    # 兼容旧版：只返回 bool
    if isinstance(result, bool):
        return (result, "")

    # 新版：返回 (handled, output)
    if isinstance(result, tuple):
        return cast(tuple[bool, str], result)

    return (True, str(result))


# ── 动态命令注册（供插件使用） ────────────────────────────────────


def register_command(name: str, module: ModuleType) -> None:
    """动态注册一条命令（供插件使用，通常在 ON_INIT 生命周期钩子中调用）

    注意：插件注入命令的正规时机是 ON_INIT（reload 重建 Agent 后会自动重跑），
    而非 on_register——后者只负责挂载钩子，不应直接操作注册表。
    与命令文件扫描不同，动态注册**不会**随插件禁用自动清理，
    调用方须在插件 on_unregister() 中成对调用 unregister_command()。

    Args:
        name: 命令名（不含斜杠，如 'office'）
        module: 实现了 name/aliases/description/execute 接口的模块

    注册后 /<name> 即可被 Agent.handle_command 识别并执行。
    与文件扫描注册的命令地位完全相同，也支持别名覆盖。
    """
    global _commands, _dynamic_names
    _dynamic_names.add(name)
    if name in _commands:
        get_logger().warning(f"⚠️  动态命令 [{name}] 与现有命令重复，已覆盖")
    _commands[name] = module

    for alias in getattr(module, "aliases", []):
        if alias in _commands:
            get_logger().warning(f"⚠️  别名 [{alias}] 冲突，已跳过")
            continue
        _commands[alias] = module


def unregister_command(name: str) -> None:
    """撤销 register_command 的动态命令注册（供插件 on_unregister 中成对调用）

    删除主名及该模块注册的全部别名——即 _commands 中所有指向
    同一模块对象的键。只影响动态注册，不触碰自动发现的文件命令
    （文件命令以文件存在为生命周期，重新发现时会重建注册表）。

    Args:
        name: 动态注册时的主命令名（如 'sc'）

    若主名不是动态命令（文件命令/不存在/别名入口），视为无操作并告警（幂等）。
    """
    global _commands, _dynamic_names
    if name not in _dynamic_names:
        get_logger().warning(f"⚠️  动态命令 [{name}] 未注册或为文件命令，unregister 无操作")
        return

    mod = _commands.get(name)
    if mod is None:
        _dynamic_names.discard(name)  # 状态不一致自愈
        get_logger().warning(f"⚠️  动态命令 [{name}] 已在注册表中缺失，仅清理登记簿")
        return

    removed = [key for key, value in _commands.items() if value is mod]
    for key in removed:
        del _commands[key]
    _dynamic_names.discard(name)
    extra = f"，含别名 {removed[1:]}" if removed[1:] else ""
    get_logger().info(f"✓ 已注销动态命令 [{name}]{extra}")
