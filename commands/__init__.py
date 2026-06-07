"""
commands/__init__.py — 命令注册表与自动发现

自动扫描 commands/ 目录下所有 .py 文件（排除 __init__.py），
导入每个模块并检查 name/execute 接口，构建命令名→模块的映射（含别名）。
"""

import asyncio
import importlib
import os
import sys

import display

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
    def execute(agent, arg: str) -> tuple[bool, str]: ...
    async def execute(agent, arg: str) -> tuple[bool, str]: ...


def _discover_commands():
    """扫描并注册所有命令模块"""
    global _commands
    _commands = {}

    commands_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(commands_dir))  # 确保项目根在 path

    for fname in sorted(os.listdir(commands_dir)):
        if not fname.endswith(".py") or fname == "__init__.py":
            continue

        mod_name = fname[:-3]  # 去掉 .py
        try:
            mod = importlib.import_module(f"commands.{mod_name}")
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

    # 清理 path
    if sys.path and sys.path[0] == os.path.dirname(commands_dir):
        sys.path.pop(0)


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
