"""
Tools 包 — 插件化工具系统

核心原则:
- bash, read_file, write_file, edit_file 必须保持直接绑定（core.py），不可插件化
- 其他工具通过 plugins/*_plugin.py 插件机制动态加载

包导出:
- ToolRegistry: 工具注册表类
- registry: 全局注册表实例
- TOOL_DEFINITIONS: 所有工具的 OpenAI schema（兼容旧代码）
- TOOL_HANDLERS: 工具处理器映射（兼容旧代码）
- dispatch(tool_name, **kwargs): 兼容旧代码的工具调度函数
- bash/read_file/write_file/edit_file/python/web_search: 快捷函数
"""

import importlib
import os
import sys
from typing import Any, Callable, Dict, List, Optional

import config


class ToolRegistry:
    """工具注册表，管理所有核心工具和插件"""
    
    def __init__(self):
        self._core_defs: List[dict] = []
        self._core_executor: Callable = None
        self._plugins: Dict[str, dict] = {}  # {name: {definition, executor}}
        self._load_core()
        self._load_plugins()
    
    def _load_core(self):
        """加载核心工具（直接绑定，不可插件化）"""
        from .core import get_core_definitions, execute_core_tool
        self._core_defs = get_core_definitions()
        self._core_executor = execute_core_tool
    
    def _load_plugins(self):
        """自动扫描并加载 plugins/ 目录下的 *_plugin.py 文件"""
        plugins_dir = os.path.join(os.path.dirname(__file__), "plugins")
        
        if not os.path.isdir(plugins_dir):
            return
        
        for fname in sorted(os.listdir(plugins_dir)):
            if not fname.endswith("_plugin.py"):
                continue
            
            plugin_name = fname[:-3]  # 去掉 .py
            # 模块名: tools.plugins.xxx_plugin → 导入需要完整包路径
            module_name = f"tools.plugins.{plugin_name}"
            
            try:
                module = importlib.import_module(f".plugins.{plugin_name}", package="tools")
                
                if hasattr(module, 'PLUGIN_DEFINITION') and hasattr(module, 'execute'):
                    self._plugins[plugin_name] = {
                        'definition': module.PLUGIN_DEFINITION,
                        'executor': module.execute,
                    }
                else:
                    print(f"[tools] ⚠️ 插件 {plugin_name} 缺少 PLUGIN_DEFINITION 或 execute，跳过")
            except Exception as e:
                print(f"[tools] ⚠️ 加载插件 {plugin_name} 失败: {e}")
    
    def get_all_definitions(self) -> List[dict]:
        """获取所有工具的 OpenAI function calling schema 列表"""
        definitions = list(self._core_defs)
        for plugin_data in self._plugins.values():
            definitions.append(plugin_data['definition'])
        return definitions
    
    def execute(self, tool_name: str, params: Dict[str, Any]) -> Any:
        """
        执行指定工具
        
        Args:
            tool_name: 工具名称
            params: 参数字典
            
        Returns:
            执行结果
        """
        # 核心工具
        core_names = {'bash', 'read_file', 'write_file', 'edit_file'}
        if tool_name in core_names:
            return self._core_executor(tool_name, params)
        
        # 插件工具（按定义中的 name 匹配）
        for plugin_data in self._plugins.values():
            def_name = plugin_data['definition']['function']['name']
            if def_name == tool_name:
                return plugin_data['executor'](params)
        
        raise ValueError(f"未知工具: {tool_name}")


# ── 全局注册表实例 ────────────────────────────────────────────────

registry = ToolRegistry()


def get_tool_definitions() -> List[dict]:
    """获取所有工具的 OpenAI schema 定义"""
    return registry.get_all_definitions()


def execute_tool(tool_name: str, params: Dict[str, Any]) -> Any:
    """执行指定工具"""
    return registry.execute(tool_name, params)


# ═══════════════════════════════════════════════════════════════════
# 兼容旧代码的导出
# ═══════════════════════════════════════════════════════════════════

TOOL_DEFINITIONS: List[dict] = get_tool_definitions()
"""所有工具的 OpenAI schema 定义列表 — 兼容旧代码的 `tools.TOOL_DEFINITIONS`"""


# 构建 TOOL_HANDLERS 字典（兼容旧代码的 `tools.dispatch` 模式）
def _build_tool_handlers() -> Dict[str, Callable]:
    """构建 TOOL_HANDLERS 映射"""
    handlers: Dict[str, Callable] = {}
    
    def make_handler(tool_name: str):
        def handler(**kwargs):
            return execute_tool(tool_name, kwargs)
        handler.__name__ = f"{tool_name}_handler"
        return handler
    
    # 核心工具
    for core_name in ['bash', 'read_file', 'write_file', 'edit_file']:
        handlers[core_name] = make_handler(core_name)
    
    # 插件工具
    for plugin_data in registry._plugins.values():
        name = plugin_data['definition']['function']['name']
        handlers[name] = make_handler(name)
    
    return handlers


TOOL_HANDLERS: Dict[str, Callable] = _build_tool_handlers()
"""工具处理器映射 {name: handler_fn} — 兼容旧代码"""


def dispatch(tool_name: str, **kwargs) -> str:
    """
    兼容旧代码的工具调度函数
    
    Args:
        tool_name: 工具名称
        **kwargs: 工具参数
        
    Returns:
        执行结果字符串
    
    示例:
        >>> dispatch('bash', command='ls -la')
        >>> dispatch('read_file', file_path='/tmp/test.txt')
    """
    if tool_name not in TOOL_HANDLERS:
        return f"错误：未知工具 '{tool_name}'"
    
    try:
        handler = TOOL_HANDLERS[tool_name]
        result = handler(**kwargs)
        return str(result) if result is not None else "执行成功（无返回）"
    except TypeError as e:
        return f"错误：工具参数错误 - {e}"
    except Exception as e:
        return f"错误：工具执行失败 - {e}"


# ═══════════════════════════════════════════════════════════════════
# 快捷函数
# ═══════════════════════════════════════════════════════════════════

def bash(command: str) -> str:
    """执行 shell 命令"""
    return execute_tool('bash', {'command': command})


def read_file(file_path: str, offset: Optional[int] = None, limit: Optional[int] = None) -> str:
    """读取文件内容"""
    params: Dict[str, Any] = {'file_path': file_path}
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
        'new_string': new_string,
    })


def python(code: str) -> str:
    """执行 Python 代码"""
    return execute_tool('python', {'code': code})


def web_search(query: str) -> str:
    """网络搜索"""
    return execute_tool('web_search', {'query': query})
