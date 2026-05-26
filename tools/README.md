# Tools Plugin System - 插件化工具系统

## 架构概述

本系统采用**核心 + 插件**的双层架构：

- **核心工具（Core Tools）**：直接绑定在代码中，不可插件化
  - `bash`: Shell 命令执行
  - `read_file`: 文件读取
  - `write_file`: 文件写入
  - `edit_file`: 精确文本替换

- **插件工具（Plugin Tools）**：动态加载的扩展功能
  - `python`: Python 代码执行
  - `web_search`: 网络搜索
  - ... (可自定义添加)

## 目录结构

```
tools/
├── __init__.py          # 插件加载器（ToolRegistry）
├── core.py              # 核心工具实现
├── plugins/             # 插件目录
│   ├── python_plugin.py
│   └── web_search_plugin.py
└── README.md            # 本文档
```

## 如何开发新插件

### 1. 创建插件文件

在 `tools/plugins/` 目录下创建 `<name>_plugin.py` 文件。

### 2. 实现必要接口

每个插件必须包含：

#### (1) PLUGIN_DEFINITION

OpenAI function calling schema 定义：

```python
PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "your_tool_name",
        "description": "工具描述",
        "parameters": {
            "type": "object",
            "properties": {
                "param1": {"type": "string", "description": "参数描述"},
                "param2": {"type": "integer", "description": "参数描述"},
            },
            "required": ["param1"],
        },
    },
}
```

#### (2) execute() 函数

执行逻辑：

```python
from typing import Dict, Any

def execute(params: Dict[str, Any]) -> str:
    """
    执行工具
    
    Args:
        params: 包含所有参数的字典
        
    Returns:
        执行结果字符串
    """
    param1 = params.get("param1")
    # ... 你的实现
    return "结果"
```

### 3. 完整示例

```python
"""
My Custom Tool Plugin
"""

from typing import Dict, Any

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "my_custom_tool",
        "description": "我的自定义工具描述",
        "parameters": {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "输入内容"},
            },
            "required": ["input"],
        },
    },
}

def execute(params: Dict[str, Any]) -> str:
    input_value = params.get("input")
    
    if not input_value:
        raise ValueError("需要 input 参数")
    
    # 处理逻辑
    result = f"处理了：{input_value}"
    
    return result
```

## 使用方式

### 获取工具定义

```python
from tools import get_tool_definitions

definitions = get_tool_definitions()
for tool in definitions:
    print(tool['function']['name'])
```

### 执行工具

```python
from tools import execute_tool

# 执行 Python 插件
result = execute_tool('python', {'code': 'print("Hello")'})

# 执行 Web Search 插件
result = execute_tool('web_search', {'query': 'Python tutorial'})

# 执行核心工具
result = execute_tool('bash', {'command': 'ls -la'})
```

## 自动加载机制

系统会在启动时自动扫描 `tools/plugins/` 目录：

1. 查找所有 `*_plugin.py` 文件
2. 尝试导入模块
3. 检查是否存在 `PLUGIN_DEFINITION` 和 `execute` 函数
4. 注册到全局注册表

**注意**：插件名称由文件名决定（去掉 `_plugin.py` 后缀）。

## 最佳实践

### ✅ 推荐做法

- 插件返回清晰的字符串结果
- 对错误情况进行友好提示
- 使用类型注解提高代码可读性
- 遵循单一职责原则

### ❌ 避免的做法

- 不要修改核心工具
- 不要在插件中抛出未捕获的异常
- 不要依赖外部状态（保持无状态）
- 不要阻塞主线程（超时控制在合理范围）

## 故障排查

### 插件未加载

检查：
1. 文件名是否符合 `*_plugin.py` 格式
2. 是否定义了 `PLUGIN_DEFINITION` 和 `execute`
3. 查看控制台日志中的加载信息

### 执行失败

检查：
1. 参数是否正确传递
2. 依赖库是否安装
3. 查看异常堆栈信息

## 未来扩展

计划中的插件方向：
- `task_create`: 任务创建与管理
- `memory_save`: 长期记忆保存
- `git_commit`: Git 提交操作
- `file_search`: 文件内容搜索
- `process_monitor`: 进程监控

---

**维护者**: Five Pebbles AI Agent  
**版本**: 1.0.0  
**最后更新**: 2024
