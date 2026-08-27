"""
Tools 包 — 插件化工具系统（全异步版本）

核心原则:
- bash, read_file, write_file, edit_file 必须保持直接绑定（core.py），不可插件化
- 其他工具通过 extensions/*_plugin.py 插件机制动态加载

包导出:
- ToolRegistry: 工具注册表类
- registry: 全局注册表实例
- dispatch(tool_name, **kwargs): 兼容旧代码的 async 工具调度函数
- bash/read_file/write_file/edit_file/python/web_search: async 快捷函数
"""

import importlib
import importlib.util
import os
from collections.abc import Awaitable, Callable
from typing import TypedDict, cast

from fp_core.logger import get_logger
from fp_core.tools.core import OpenAISchema

# 工具执行器签名：所有工具 handler 都是 async def(params: dict) -> str
ToolExecutor = Callable[..., Awaitable[str]]


class PluginEntry(TypedDict):
    """已注册的插件工具条目"""

    definition: OpenAISchema
    executor: ToolExecutor
    source: str


class ToolRegistry:
    """工具注册表，管理所有核心工具和插件"""

    def __init__(self):
        self._core_defs: list[OpenAISchema] = []
        self._core_executor: Callable[[str, dict[str, object]], Awaitable[str]] | None = None
        self._plugins: dict[str, PluginEntry] = {}  # {name: {definition, executor, source}}
        self._load_core()
        self._load_plugins()

    def _load_core(self):
        """加载核心工具（直接绑定，不可插件化）"""
        from fp_core.tools.core import execute_core_tool, get_core_definitions

        self._core_defs = get_core_definitions()
        self._core_executor = execute_core_tool

    def _load_plugins(self):
        """自动扫描并加载插件（内置 → 三来源，同名覆盖 + 警告）"""
        builtin_dir = os.path.join(os.path.dirname(__file__), "extensions")
        self._load_from_dir(builtin_dir, "fp_core.tools.extensions")

        # 三来源用户工具目录（fetched → public → private，后加载覆盖先加载 + 警告）
        # 优先级：private > public > fetched（_load_from_dir 内同名覆盖打警告）
        from fp_core.config import user_dirs

        for user_dir in user_dirs("tools"):
            self._load_from_dir(user_dir)

    def _load_from_dir(self, directory: str, package_prefix: str | None = None):
        """从指定目录加载插件工具"""
        if not os.path.isdir(directory):
            return

        for fname in sorted(os.listdir(directory)):
            if not fname.endswith("_plugin.py"):
                continue

            plugin_name = fname[:-3]

            # 同名覆盖警告（三来源后加载覆盖先加载；内置被用户覆盖也在此提醒）
            if any(k == plugin_name or k.startswith(f"{plugin_name}/") for k in self._plugins):
                get_logger().warning(f"[tools] ⚠️ 同名工具插件覆盖: {plugin_name}")

            try:
                if package_prefix:
                    module = importlib.import_module(f".extensions.{plugin_name}", package="fp_core.tools")
                else:
                    spec = importlib.util.spec_from_file_location(plugin_name, os.path.join(directory, fname))
                    if spec is None or spec.loader is None:
                        continue
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                # ── 多工具模式：PLUGIN_DEFINITIONS + TOOL_MAP ──
                if hasattr(module, "PLUGIN_DEFINITIONS") and hasattr(module, "TOOL_MAP"):
                    definitions = cast(list[OpenAISchema], module.PLUGIN_DEFINITIONS)
                    tool_map = cast(dict[str, ToolExecutor], module.TOOL_MAP)
                    module_execute = cast(ToolExecutor, module.execute)
                    for defn in definitions:
                        tool_name = defn["function"]["name"]
                        executor = tool_map.get(tool_name, module_execute)
                        self._plugins[f"{plugin_name}/{tool_name}"] = {
                            "definition": defn,
                            "executor": executor,
                            "source": plugin_name,
                        }

                # ── 单工具模式：PLUGIN_DEFINITION + execute ──
                elif hasattr(module, "PLUGIN_DEFINITION") and hasattr(module, "execute"):
                    self._plugins[plugin_name] = {
                        "definition": cast(OpenAISchema, module.PLUGIN_DEFINITION),
                        "executor": cast(ToolExecutor, module.execute),
                        "source": plugin_name,
                    }
                else:
                    get_logger().warning(f"[tools] ⚠️ 插件 {plugin_name} 缺少 PLUGIN_DEFINITION 或 execute，跳过")
            except Exception as e:
                get_logger().warning(f"[tools] ⚠️ 加载插件 {plugin_name} 失败: {e}")

    def register_tool(self, name: str, definition: OpenAISchema, executor: ToolExecutor):
        """动态注册一个工具（供生命周期插件使用）

        Args:
            name: 工具名称（如 'task_create'）
            definition: OpenAI function calling schema dict
            executor: 异步处理函数，签名 async def(params: dict) -> str
        """
        self._plugins[f"lifecycle/{name}"] = {
            "definition": definition,
            "executor": executor,
            "source": "lifecycle_plugin",
        }

    def get_all_definitions(self) -> list[OpenAISchema]:
        """获取所有工具的 OpenAI function calling schema 列表"""
        definitions = list(self._core_defs)
        for plugin_data in self._plugins.values():
            definitions.append(plugin_data["definition"])
        return definitions

    async def execute(self, tool_name: str, params: dict[str, object]) -> str:
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


def create_registry() -> ToolRegistry:
    """创建独立的 ToolRegistry 实例（核心工具 + 重新扫描插件）。

    与全局 `registry` 完全隔离，适用于多 Agent 实例场景。
    """
    return ToolRegistry()


async def execute_tool(tool_name: str, params: dict[str, object]) -> str:
    """执行指定工具（异步）"""
    return await registry.execute(tool_name, params)
