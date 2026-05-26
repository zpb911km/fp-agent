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


def task_clear() -> str:
    """清除所有已完成的任务"""
    return execute_tool('task_clear', {})


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


# ── 任务系统初始化 ───────────────────────────────────────────────

import json
from pathlib import Path

# 全局任务状态存储 (在内存中保持最新状态)
_tasks_state = {
    "tasks": [],
    "next_id": 1,
    "tasks_file": None
}


def init_tasks(tasks_file: str) -> None:
    """
    初始化任务系统
    
    Args:
        tasks_file: 任务数据存储文件路径
        
    Returns:
        None
    """
    global _tasks_state
    tasks_path = Path(tasks_file)
    _tasks_state["tasks_file"] = str(tasks_path)
    
    # 确保目录存在
    tasks_path.parent.mkdir(parents=True, exist_ok=True)
    
    # 如果文件不存在，创建空的任务列表
    if not tasks_path.exists():
        with open(tasks_path, 'w', encoding='utf-8') as f:
            json.dump({"tasks": [], "next_id": 1}, f, ensure_ascii=False, indent=2)
        print(f"✅ 已创建任务文件：{tasks_file}")
    else:
        print(f"📂 任务文件已存在：{tasks_file}")
        # 从文件加载任务状态
        try:
            with open(tasks_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                _tasks_state["tasks"] = data.get("tasks", [])
                _tasks_state["next_id"] = data.get("next_id", 1)
            print(f"📋 已加载 {_tasks_state['next_id']-1} 个任务")
        except Exception as e:
            print(f"⚠️ 加载任务失败：{e}")


def _reload_tasks():
    """从文件重新加载任务状态到内存（避免插件直接写文件导致的不同步）"""
    global _tasks_state
    if not _tasks_state.get("tasks_file"):
        return
    tasks_path = Path(_tasks_state["tasks_file"])
    if tasks_path.exists():
        try:
            with open(tasks_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                _tasks_state["tasks"] = data.get("tasks", [])
                _tasks_state["next_id"] = data.get("next_id", 1)
        except Exception:
            pass


def get_pending_tasks() -> list[dict]:
    """
    获取所有 pending 状态的任务（每次调用前重新从文件加载，确保与插件操作同步）
    
    Returns:
        pending 任务列表，按 id 排序
    """
    # 重新从文件加载，确保与 task_update/create 插件操作同步
    _reload_tasks()
    tasks = [t for t in _tasks_state["tasks"] if t.get("status") == "pending"]
    return sorted(tasks, key=lambda x: x.get("id", 0))


def save_tasks() -> None:
    """
    先将文件中的最新状态加载到内存（合并插件改动），再持久化到文件
    
    Returns:
        None
    """
    if not _tasks_state["tasks_file"]:
        return
    
    # 先同步文件中的最新状态（插件可能已修改），避免覆写
    _reload_tasks()
    
    tasks_path = Path(_tasks_state["tasks_file"])
    data = {
        "tasks": _tasks_state["tasks"],
        "next_id": _tasks_state["next_id"]
    }
    with open(tasks_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"💾 任务已保存 ({len(_tasks_state['tasks'])} 个任务)")
