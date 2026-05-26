"""
Tools Plugin System - 插件化工具系统

核心原则:
- bash, read_file, write_file, edit_file 必须保持直接绑定，不可插件化
- 其他工具通过插件机制动态加载
"""

import importlib
import os
from typing import Dict, Any, List, Callable
from pathlib import Path


class ToolRegistry:
    """工具注册表，管理所有插件和核心工具"""
    
    def __init__(self):
        self._plugins: Dict[str, dict] = {}
        self._core_tools: Dict[str, Callable] = {}
        self._load_core_tools()
        self._load_plugins()
    
    def _load_core_tools(self):
        """加载核心工具（直接绑定，不可插件化）"""
        from .core import get_core_definitions, execute_core_tool
        self._core_defs = get_core_definitions()
        self._core_executors = execute_core_tool
    
    def _load_plugins(self):
        """自动加载 plugins 目录下的所有插件"""
        plugins_dir = Path(__file__).parent / "plugins"
        
        if not plugins_dir.exists():
            print(f"⚠️ 插件目录不存在：{plugins_dir}")
            return
        
        for plugin_file in plugins_dir.glob("*_plugin.py"):
            plugin_name = plugin_file.stem.replace("_plugin", "")
            try:
                module = importlib.import_module(f".plugins.{plugin_name}_plugin", package="tools")
                
                if hasattr(module, 'PLUGIN_DEFINITION') and hasattr(module, 'execute'):
                    self._plugins[plugin_name] = {
                        'definition': module.PLUGIN_DEFINITION,
                        'executor': module.execute,
                        'module': module
                    }
                    print(f"✅ 已加载插件：{plugin_name}")
                else:
                    print(f"⚠️ 插件 {plugin_name} 缺少必要接口，跳过")
                    
            except Exception as e:
                print(f"❌ 加载插件 {plugin_name} 失败：{e}")
    
    def get_all_definitions(self) -> List[dict]:
        """获取所有工具的 OpenAI schema 定义"""
        definitions = list(self._core_defs)
        for plugin_name, plugin_data in self._plugins.items():
            definitions.append(plugin_data['definition'])
        return definitions
    
    def execute(self, tool_name: str, params: Dict[str, Any]) -> Any:
        """执行指定工具"""
        # 优先检查核心工具
        if tool_name in ['bash', 'read_file', 'write_file', 'edit_file']:
            return self._core_executors(tool_name, params)
        
        # 然后检查插件（通过定义中的 name 字段查找）
        for plugin_key, plugin_data in self._plugins.items():
            if plugin_data['definition']['function']['name'] == tool_name:
                return plugin_data['executor'](params)
        
        raise ValueError(f"未找到工具：{tool_name}")


# 全局注册表实例
registry = ToolRegistry()


def get_tool_definitions() -> List[dict]:
    """获取所有工具的 OpenAI schema 定义"""
    return registry.get_all_definitions()


def execute_tool(tool_name: str, params: Dict[str, Any]) -> Any:
    """执行指定工具"""
    return registry.execute(tool_name, params)


# 兼容旧代码的导出
TOOL_DEFINITIONS = get_tool_definitions()


# 快捷函数（核心工具）
def bash(command: str) -> str:
    """执行 shell 命令"""
    return execute_tool('bash', {'command': command})


def read_file(file_path: str, offset: int = None, limit: int = None) -> str:
    """读取文件内容"""
    params = {'file_path': file_path}
    if offset is not None:
        params['offset'] = offset
    if limit is not None:
        params['limit'] = limit
    return execute_tool('read_file', params)


def write_file(file_path: str, content: str) -> str:
    """写入文件"""
    return execute_tool('write_file', {'file_path': file_path, 'content': content})


def edit_file(file_path: str, old_string: str, new_string: str) -> str:
    """编辑文件（精确替换）"""
    return execute_tool('edit_file', {
        'file_path': file_path,
        'old_string': old_string,
        'new_string': new_string
    })


# 插件工具的快捷函数（可选）
def python(code: str) -> str:
    """执行 Python 代码"""
    return execute_tool('python', {'code': code})


def web_search(query: str) -> str:
    """网络搜索"""
    return execute_tool('web_search', {'query': query})


# ── 兼容旧代码的 dispatch 函数 ───────────────────────────────────────────────


def dispatch(tool_name: str, **kwargs) -> str:
    """
    兼容旧代码的工具调度函数
    
    Args:
        tool_name: 工具名称
        **kwargs: 工具参数
        
    Returns:
        执行结果字符串
    """
    return execute_tool(tool_name, kwargs)


