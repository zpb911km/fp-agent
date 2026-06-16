---
name: "subagent"
title: "子代理派遣 — Token 经济学的核心武器"
description: "将多步骤、文件密集、分析型任务委派给子代理。子代理拥有独立上下文，不占主上下文 token。同一子任务内 prefix 稳定 → API 缓存命中 → 半价。"
category: "core"
version: "2.0"
priority: 99
---

# 子代理派遣（subagent）

## 何时用

**用 subagent** — 需要 ≥2 次工具调用、读大文件、多步分析、批量处理、代码调试、信息检索综合。

**不用 subagent** — 单步查询、简单对话、工具结果 <200 token。

## 参数说明

| 参数 | 必填 | 说明 |
|------|------|------|
| `task` | ✅ | 任务描述，清晰完整自包含 |
| `cwd` | ✅ | 工作目录，通过 `pwd` 获取 |
| `context` | 推荐 | 传递背景上下文（子代理无对话历史） |
| `store_result` | 可选 | 结果自动存入记忆，之后 `memory_read` 读取 |
| `timeout` | 可选 | 默认 300s，范围 10~900 |
| `constraints.verbose` | 可选 | 默认 false（静默模式，只返回结论）；true（调试模式，输出完整推理） |
| `constraints.output_format` | 可选 | text/json/markdown |
| `constraints.max_length` | 可选 | 结果最大字符数 |

## 要点

1. `task` 要自包含，所有背景通过 `context` 传入
2. 复杂分析用 `output_format=json`，方便主代理解析
3. 默认静默模式（`verbose=false`），子代理推理链不进入主上下文
4. `store_result` 搭配 `memory_read` 实现跨步骤数据传递
