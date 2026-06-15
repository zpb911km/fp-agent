"""
commands/__init__.py — 命令注册表与自动发现

自动扫描 commands/ 目录下所有 .py 文件（排除 __init__.py），
导入每个模块并检查 name/execute 接口，构建命令名→模块的映射（含别名）。
"""

import asyncio
import importlib
import importlib.util
import os

from fp_core import display
from fp_core.platform_utils import get_data_dir

# 缓存：命令名 → 模块对象
_commands: dict[str, "CommandModule"] = {}


# 类型标注
class CommandModule:
    name: str
    aliases: list[str]
    description: str

    # execute 返回 (已处理, 输出文本)；
    # 兼容旧版：也可只返回 bool（自动转为 ("", False/True)）
    # 同步或异步均可，由 execute() 自动适配
    async def execute(self, arg: str) -> tuple[bool, str]: ...


def _discover_commands():
    """扫描并注册所有命令模块（内置 → 用户，同名覆盖）"""
    global _commands
    _commands = {}

    builtin_dir = os.path.dirname(os.path.abspath(__file__))
    _scan_dir(builtin_dir, "fp_core.commands")

    # 用户命令目录（跨平台：Linux ~/.local/share/fp/commands, Windows %LOCALAPPDATA%/fp/commands）
    user_dir = os.path.join(get_data_dir(), "commands")
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
            display.warning(f"⚠️  命令加载失败 [{mod_name}]: {e}")
            continue

        # 校验接口
        if not hasattr(mod, "name") or not hasattr(mod, "execute"):
            display.warning(f"⚠️  命令模块 [{mod_name}] 缺少 name/execute，已跳过")
            continue

        name = mod.name
        if name in _commands:
            display.warning(f"⚠️  命令 [{name}] 重复定义，已覆盖")
        _commands[name] = mod

        # 注册别名
        for alias in getattr(mod, "aliases", []):
            if alias in _commands:
                display.warning(f"⚠️  别名 [{alias}] 与已有命令/别名冲突，已跳过")
                continue
            _commands[alias] = mod


_discover_commands()


def get_command(name: str) -> CommandModule | None:
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


async def execute(agent, cmd_name: str, arg: str) -> tuple[bool, str]:
    """执行命令，返回 (是否已处理, 输出文本)。

    兼容旧版只返回 bool 的命令（自动补为 ("", False/True)）。
    新版命令可返回 tuple[bool, str] 或 tuple[bool, str, str]。
    """
    mod = get_command(cmd_name)
    if mod is None:
        return (False, "")

    # 执行命令（自动适配同步/异步）
    if asyncio.iscoroutinefunction(mod.execute):
        result = await mod.execute(agent, arg)
    else:
        result = mod.execute(agent, arg)

    # 兼容旧版：只返回 bool
    if isinstance(result, bool):
        return (result, "")

    # 新版：返回 (handled, output)
    if isinstance(result, tuple):
        return result

    return (True, str(result))
