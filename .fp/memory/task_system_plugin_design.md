---
name: task_system_plugin_design
description: fp-core 任务系统插件最终设计 — plugin 架构、钩子、工具、提醒机制
type: project
created: 2026-07-11 01:16
---

# Task System Plugin — 最终设计

## 定位
fp_core/plugins/task_system/ — 一个完整的 Plugin 子系统，零入侵 core/tools。

## 文件结构
fp_core/plugins/task_system/
├── __init__.py       # 导出 TaskSystemPlugin
├── plugin.py         # Plugin 子类：ON_INIT → 注入 system prompt + 注册工具
│                    #            ON_BEFORE_LLM_CALL → 附加 [task] 提醒
├── models.py         # Task dataclass(id, subject, status) + TaskStatus 枚举
├── store.py          # TaskStore — JSON CRUD，文件 .fp/tasks.json
└── tools.py          # 4 个工具处理函数（task_create/update/list/clear）

## 数据模型
Task { id: int, subject: str, status: "pending"|"in_progress"|"completed" }
文件: .fp/tasks.json — JSON 格式，方便解析

## 2 个生命周期钩子

### 钩子 1: ON_INIT
### 钩子 2: ON_BEFORE_LLM_CALL
- 每次 LLM 调用前，在 messages 末尾追加一行 system 消息
- 格式: [task] ▶#N ⬜M  或  [task] ⬜M  或  [task] ▶#N  或 不附加
- 占用约 17 字节，频率失控代价可忽略

## 4 个工具（通过 ON_INIT 中调用 registry.register_tool 注册）
task_create(subject)   — 创建新任务
task_update(task_id, status) — 更新状态
task_list              — 查看清单
task_clear             — 清理已完成

## 修改要点
### 新增
- fp_core/plugins/task_system/ 插件包（4 个文件）
- ToolRegistry.register_tool() 方法（被动注册接口）
- prompt_builder 支持 ON_INIT 回传内容注入 system prompt

### 删除
- fp_core/tools/extensions/task_*_plugin.py（4 个旧工具文件）
- agent.md 中任务系统相关文本（由插件注入）

### 零改动
- core/agent.py
- core/lifecycle.py（已有足够钩子）
- tools/__init__.py（只加方法，不改现有逻辑）
