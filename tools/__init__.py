"""
Tools 包 — 插件化工具系统（全异步版本）

核心原则:
- bash, read_file, write_file, edit_file 必须保持直接绑定（core.py），不可插件化
- 其他工具通过 plugins/*_plugin.py 插件机制动态加载

包导出:
- ToolRegistry: 工具注册表类
- registry: 全局注册表实例
- dispatch(tool_name, **kwargs): 兼容旧代码的 async 工具调度函数
- bash/read_file/write_file/edit_file/python/web_search: async 快捷函数
"""

import asyncio
import importlib
import os
import sys
from collections.abc import Callable
from typing import Any, Optional

import config


class ToolRegistry:
    """工具注册表，管理所有核心工具和插件"""

    def __init__(self):
        self._core_defs: list[dict] = []
        self._core_executor: Callable | None = None
        self._plugins: dict[str, dict] = {}  # {name: {definition, executor}}
        self._load_core()
        self._load_plugins()

    def _load_core(self):
        """加载核心工具（直接绑定，不可插件化）"""
        from .core import execute_core_tool, get_core_definitions

        self._core_defs = get_core_definitions()
        self._core_executor = execute_core_tool

    def _load_plugins(self):
        """自动扫描并加载 plugins/ 目录下的 *_plugin.py 文件

        每个插件文件可以导出:
        - PLUGIN_DEFINITIONS (list[dict]) + TOOL_MAP (dict[str, callable]) — 多工具
        - PLUGIN_DEFINITION (dict) + execute (callable) — 单工具（传统方式）
        """
        plugins_dir = os.path.join(os.path.dirname(__file__), "plugins")

        if not os.path.isdir(plugins_dir):
            return

        for fname in sorted(os.listdir(plugins_dir)):
            if not fname.endswith("_plugin.py"):
                continue

            plugin_name = fname[:-3]

            try:
                module = importlib.import_module(f".plugins.{plugin_name}", package="tools")

                # ── 多工具模式：PLUGIN_DEFINITIONS + TOOL_MAP ──
                if hasattr(module, "PLUGIN_DEFINITIONS") and hasattr(module, "TOOL_MAP"):
                    for defn in module.PLUGIN_DEFINITIONS:
                        tool_name = defn["function"]["name"]
                        executor = module.TOOL_MAP.get(tool_name, module.execute)
                        self._plugins[f"{plugin_name}/{tool_name}"] = {
                            "definition": defn,
                            "executor": executor,
                            "source": plugin_name,
                        }

                # ── 单工具模式：PLUGIN_DEFINITION + execute ──
                elif hasattr(module, "PLUGIN_DEFINITION") and hasattr(module, "execute"):
                    self._plugins[plugin_name] = {
                        "definition": module.PLUGIN_DEFINITION,
                        "executor": module.execute,
                        "source": plugin_name,
                    }
                else:
                    print(f"[tools] ⚠️ 插件 {plugin_name} 缺少 PLUGIN_DEFINITION 或 execute，跳过")
            except Exception as e:
                print(f"[tools] ⚠️ 加载插件 {plugin_name} 失败: {e}")

    def get_all_definitions(self) -> list[dict]:
        """获取所有工具的 OpenAI function calling schema 列表"""
        definitions = list(self._core_defs)
        for plugin_data in self._plugins.values():
            definitions.append(plugin_data["definition"])
        return definitions

    async def execute(self, tool_name: str, params: dict[str, Any]) -> Any:
        """
        执行指定工具（异步）

        Args:
            tool_name: 工具名称
            params: 参数字典

        Returns:
            执行结果
        """
        # 核心工具
        core_names = {"bash", "read_file", "write_file", "edit_file"}
        if tool_name in core_names:
            if self._core_executor is None:
                raise RuntimeError("核心工具未初始化，请先调用 _load_core()")
            return await self._core_executor(tool_name, params)

        # 插件工具（按定义中的 name 匹配）
        for plugin_data in self._plugins.values():
            def_name = plugin_data["definition"]["function"]["name"]
            if def_name == tool_name:
                return await plugin_data["executor"](params)

        raise ValueError(f"未知工具: {tool_name}")


# ── 全局注册表实例 ────────────────────────────────────────────────

registry = ToolRegistry()


async def execute_tool(tool_name: str, params: dict[str, Any]) -> Any:
    """执行指定工具（异步）"""
    return await registry.execute(tool_name, params)


# ═══════════════════════════════════════════════════════════════════
# 兼容旧代码的导出
# ═══════════════════════════════════════════════════════════════════

# TOOL_HANDLERS 不再维护同步映射，改用异步 dispatch 函数


async def dispatch(tool_name: str, **kwargs) -> str:
    """
    工具调度函数（异步）

    Args:
        tool_name: 工具名称
        **kwargs: 工具参数

    Returns:
        执行结果字符串
    """
    try:
        result = await execute_tool(tool_name, kwargs)
        return str(result) if result is not None else "执行成功（无返回）"
    except TypeError as e:
        return f"错误：工具参数错误 - {e}"
    except Exception as e:
        return f"错误：工具执行失败 - {e}"


# ═══════════════════════════════════════════════════════════════════
# async 快捷函数
# ═══════════════════════════════════════════════════════════════════


async def bash(command: str) -> str:
    """执行 shell 命令"""
    return await execute_tool("bash", {"command": command})


async def read_file(file_path: str, offset: int | None = None, limit: int | None = None) -> str:
    """读取文件内容"""
    params: dict[str, Any] = {"file_path": file_path}
    if offset is not None:
        params["offset"] = offset
    if limit is not None:
        params["limit"] = limit
    return await execute_tool("read_file", params)


async def write_file(file_path: str, content: str) -> str:
    """写入文件"""
    return await execute_tool("write_file", {"file_path": file_path, "content": content})


async def edit_file(file_path: str, old_string: str, new_string: str) -> str:
    """编辑文件（精确替换）"""
    return await execute_tool(
        "edit_file",
        {
            "file_path": file_path,
            "old_string": old_string,
            "new_string": new_string,
        },
    )


async def python(code: str) -> str:
    """执行 Python 代码"""
    return await execute_tool("python", {"code": code})


async def web_search(query: str) -> str:
    """网络搜索"""
    return await execute_tool("web_search", {"query": query})
