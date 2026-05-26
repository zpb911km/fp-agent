# 工具插件开发指南

## 📁 目录结构

```
tools/
├── __init__.py      # 插件加载器和注册表
├── core.py          # 核心工具实现（不可修改）
└── plugins/         # 插件目录
    ├── python_plugin.py
    ├── web_search_plugin.py
    ├── task_plugin.py
    └── {your_tool}_plugin.py  # 自定义插件
```

## 🔧 核心原则

1. **核心工具不可变**：以下 4 个工具必须保持直接绑定，不可插件化
   - `bash` - Shell 命令执行
   - `read_file` - 文件读取
   - `write_file` - 文件写入
   - `edit_file` - 精确文本替换

2. **插件动态扩展**：其他所有工具都可以通过插件机制添加

## 📝 插件开发规范

### 1. 文件名规范

插件文件必须命名为 `{tool_name}_plugin.py`，例如：
- `python_plugin.py` → 工具名：`python`
- `web_search_plugin.py` → 工具名：`web_search`
- `task_plugin.py` → 工具名：`task_create`

### 2. 必需接口

每个插件必须实现两个部分：

#### (1) PLUGIN_DEFINITION

OpenAI function calling schema 定义：

```python
PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "tool_name",  # 工具的唯一标识符
        "description": "工具描述",
        "parameters": {
            "type": "object",
            "properties": {
                "param1": {"type": "string", "description": "参数描述"},
                "param2": {"type": "integer", "description": "参数描述"},
            },
            "required": ["param1"],  # 必填参数列表
        },
    },
}
```

#### (2) execute(params) 函数

执行逻辑函数：

```python
def execute(params: dict) -> str:
    """
    执行工具
    
    Args:
        params: 包含所有必需参数的字典
        
    Returns:
        执行结果字符串
    """
    # 你的实现代码
    pass
```

### 3. 完整示例

```python
"""
My Custom Tool Plugin

通过临时文件方式执行用户提供的 Python 代码，适合复杂数据处理和算法验证。
"""

import subprocess
import tempfile
import os


PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "my_custom_tool",
        "description": "这是一个自定义工具的详细描述",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "要执行的代码"},
                "timeout": {"type": "integer", "description": "超时时间（秒）"}
            },
            "required": ["code"],
        },
    },
}


def execute(params: dict) -> str:
    """执行自定义工具"""
    code = params.get("code")
    timeout = params.get("timeout", 30)
    
    if not code:
        raise ValueError("my_custom_tool 需要 code 参数")
    
    # 创建临时文件
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(code)
        temp_path = f.name
    
    try:
        # 执行代码
        result = subprocess.run(
            ['python3', temp_path],
            capture_output=True,
            text=True,
            timeout=timeout
        )
        
        output = result.stdout
        if result.stderr:
            output += f"\n[错误]\n{result.stderr}"
        
        return output if output else "[无输出]"
        
    finally:
        # 清理临时文件
        os.unlink(temp_path)
```

## 🚀 快速开始

1. 在 `tools/plugins/` 目录下创建新文件：`my_tool_plugin.py`
2. 实现 `PLUGIN_DEFINITION` 和 `execute(params)`
3. 重启应用，插件会自动加载
4. 测试：`execute_tool('my_tool_name', {'param': 'value'})`

## 🧪 测试插件

```python
from tools import get_tool_definitions, execute_tool

# 查看所有可用工具
defs = get_tool_definitions()
for d in defs:
    print(f"{d['function']['name']}: {d['function']['description']}")

# 执行插件
result = execute_tool('my_tool_name', {'param': 'value'})
print(result)
```

## ⚠️ 注意事项

1. **异常处理**：插件应该捕获并处理所有可能的异常，返回友好的错误消息
2. **资源清理**：使用临时文件时记得在 `finally` 块中清理
3. **超时控制**：长时间运行的操作应设置合理的超时时间
4. **安全性**：不要执行不可信的代码，避免安全漏洞
5. **文档注释**：为插件添加清晰的 docstring 和说明

## 📦 现有插件列表

| 插件名 | 功能 | 状态 |
|--------|------|------|
| python | 执行 Python 代码 | ✅ 已实现 |
| web_search | Web 搜索 | ✅ 已实现 |
| task_create | 任务管理 | ✅ 已实现 |


**提示**：遵循这些规范可以确保插件与系统无缝集成！
