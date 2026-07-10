---
name: codegraph_query_tool
description: 基于 AST 按需热分析的泛用代码知识图谱查询工具，支持任意项目
type: project
created: 2026-07-11 01:16
---

已重构 codegraph_query 工具插件 (tools/extensions/codegraph_query_plugin.py)。
方案 C：不建索引，不存缓存，每次查询只 AST 解析最相关的文件。
泛用性：通过 project_path 参数指定任意项目目录（默认当前工作目录）。

核心能力：
- 文件结构查询: "xxx.py 有哪些类？"
- 类方法查询: "XXX 类有哪些方法？"
- 符号定义查询: "xxx 函数的定义"
- 调用者追踪: "谁调用了 xxx()？"
- 导入关系分析: "xxx.py 导入了哪些模块？"
- 反向依赖查询: "谁导入了 xxx？"
- 影响范围分析: "改 xxx.py 影响谁？"
- 调用链分析: "xxx 的调用链"
- 全文搜索: "搜索 xxx"
- 所有模块列表: "列出所有模块"

使用方式：
- 分析当前项目: codegraph_query("agent.py")
- 分析其他项目: codegraph_query("main.py", project_path="/other/project")

技术栈：纯 Python 标准库（ast + re + pathlib），零外部依赖。
已同步修改 prompts/agent.md。
