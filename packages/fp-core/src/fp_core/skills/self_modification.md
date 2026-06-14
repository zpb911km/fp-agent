---
name: "self_modification"
title: "修改自身源代码并测试验证"
description: "智能体可以修改自己的源代码，但必须遵循严格的测试流程：1. 使用 task_create 列出所有步骤；2. 按顺序执行任务；3. 通过 echo '测试内容' | fp 注入提示词到新实例进行验证；4. 如果测试失败则 git 回滚，成功则 git 提交。此技能确保代码修改的安全性和可追溯性。"
category: "maintenance"
version: "1.0"
priority: 1
---

[技能] 修改自身源代码并测试验证
智能体可以修改自己的源代码，但必须遵循严格的测试流程：
1. 使用 task_create 列出所有步骤
2. 按顺序执行任务
3. 通过 echo "测试内容" | python3 {{path_to_agent_file}} 注入提示词到新实例进行验证
4. 如果测试失败则 git 回滚，成功则 git 提交
此技能确保代码修改的安全性和可追溯性。
