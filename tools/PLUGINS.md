# Tools Plugin System - 插件系统文档

## 📋 架构概述

本系统采用**核心 + 插件**的双层架构：

### 核心工具（直接绑定）

以下 4 个工具必须保持直接绑定在 `core.py` 中，作为系统的基础设施：

| 工具 | 功能 | 说明 |
|------|------|------|
| `bash` | Shell 命令执行 | 支持任意 bash 语法 |
| `read_file` | 文件读取 | 支持行范围指定 |
| `write_file` | 文件写入 | 自动创建目录 |
| `edit_file` | 精确文本替换 | 只替换首次出现 |

### 插件工具（动态加载）

以下工具已通过插件形式实现：

| 文件名 | 工具名 | 功能 |
|--------|--------|------|
| `python_plugin.py` | `python` | 执行 Python 代码 |
| `web_search_plugin.py` | `web_search` | 网络搜索 (DuckDuckGo) |
| `web_fetch_plugin.py` | `web_fetch` | 网页抓取和解析 |
| `task_create_plugin.py` | `task_create` | 创建任务 |
| `task_update_plugin.py` | `task_update` | 更新任务状态 |
| `task_list_plugin.py` | `task_list` | 列出所有任务 |
| `memory_save_plugin.py` | `memory_save` | 保存记忆 |
| `memory_read_plugin.py` | `memory_read` | 读取记忆 |

## 🛠️ 插件开发指南

### 1. 创建插件文件

在 `tools/plugins/` 目录下创建 `<name>_plugin.py` 文件。

### 2. 实现必要接口

每个插件必须包含：

#### (1) PLUGIN_DEFINITION

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
            },
            "required": ["param1"],
        },
    },
}
```

#### (2) execute() 函数

```python
def execute(params: dict) -> str:
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

参考 `python_plugin.py` 或 `web_fetch_plugin.py`。

## 🔧 使用方式

### 获取所有工具定义

```python
from tools import get_tool_definitions

definitions = get_tool_definitions()
for tool in definitions:
    print(tool['function']['name'])
```

### 执行工具

```python
from tools import execute_tool, dispatch

# 执行 Python 插件
result = dispatch('python', code='print("Hello")')

# 或者使用 execute_tool
result = execute_tool('python', {'code': 'print("Hello")'})
```

## 📂 插件列表详情

### python_plugin.py

- **功能**: 执行 Python 代码
- **超时**: 30 秒
- **依赖**: `python3`

### web_search_plugin.py

- **功能**: 网络搜索
- **API**: DuckDuckGo
- **注意**: 推荐使用 `ddgs` 替代 `duckduckgo_search`

### web_fetch_plugin.py

- **功能**: 抓取并解析网页内容
- **超时**: 15 秒
- **依赖**: `requests`, `re`

### task_create_plugin.py

- **功能**: 创建新任务
- **数据持久化**: `data/tasks.json`

### task_update_plugin.py

- **功能**: 更新任务状态
- **状态枚举**: `pending`, `in_progress`, `completed`

### task_list_plugin.py

- **功能**: 列出所有任务及其状态

### memory_save_plugin.py

- **功能**: 保存长期记忆
- **存储格式**: Markdown + YAML frontmatter
- **存储位置**: `data/memory/`

### memory_read_plugin.py

- **功能**: 读取/搜索记忆
- **支持操作**: 列出全部或关键词搜索

## 🚀 扩展性

### 如何添加新插件

1. 在 `tools/plugins/` 创建 `<name>_plugin.py`
2. 定义 `PLUGIN_DEFINITION`
3. 实现 `execute()` 函数
4. 重启应用即可自动加载

### 插件命名规范

- 文件名必须是 `<name>_plugin.py` 格式
- `PLUGIN_DEFINITION.function.name` 必须与文件名一致（去掉 `_plugin.py`）

## ⚡ 性能优化

- 插件启动时一次性加载
- 工具定义缓存在内存中
- 支持懒加载外部依赖

## 🛡️ 错误处理

- 插件加载失败不影响核心功能
- 插件执行异常返回错误消息字符串
- 清晰的错误提示信息

---

**版本**: 1.0  
**最后更新**: 2026-05-26  
**维护者**: Five Pebbles AI Agent
