---
name: tools_analysis_report
description: [subagent] 深入分析 Tools 模块的导入、注册和覆写逻辑，输出分析报告。
type: reference
created: 2026-07-11 01:16
---

分析报告原文较长，核心结论：ToolRegistry 双层加载——核心工具（core.py 硬绑定 4 个：bash/read_file/write_file/edit_file）+ 插件工具（plugins/*_plugin.py 自动扫描）。两种插件模式：单工具（PLUGIN_DEFINITION + execute，11/12 个插件）和多工具（PLUGIN_DEFINITIONS + TOOL_MAP，仅 kdeconnect_plugin）。覆写通过 dict key 覆盖实现，核心工具不可覆写（独立 _core_executor 分发）。用户目录 ~/.local/share/fp/tools/extensions/ 通过 spec_from_file_location 路径加载。支持全局单例 registry 和独立实例 create_registry()。ToolExecutor 默认创建独立实例。
