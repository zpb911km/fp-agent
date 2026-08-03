# FP Agent 文档中心

> **文档根目录** — 全项目文档统一入口

---

## 📋 目录

| 分类 | 文档 | 描述 |
|------|------|------|
| 🤖 **自举手册** | [self/README.md](self/README.md) | **FP 自己阅读的索引**（分发后拓展自身的能力边界+查找地图） |
| | [self/扩展工具.md](self/扩展工具.md) | 工具 Tool（FP 主动调用，自举核心）— 加载器+范例 |
| | [self/扩展命令.md](self/扩展命令.md) | 命令 Command（面向用户的 /xxx，FP 不可主动调用） |
| | [self/扩展插件.md](self/扩展插件.md) | 插件 Plugin（后台钩子，横切关注点，慎写） |
| | [self/技能与记忆.md](self/技能与记忆.md) | 记忆 Memory（FP 主动读写的知识库） |
| 📖 **使用指南** | [guide/快速开始.md](guide/快速开始.md) | 安装→初始化→内置系统→插件扩展，全流程入门 |
| | [guide/配置指南.md](guide/配置指南.md) | config.json 字段详解、环境变量 |
| | [guide/CLI入门.md](guide/CLI入门.md) | 基本用法、命令行参数、快捷键 |
| | [guide/命令参考.md](guide/命令参考.md) | 15 个内置命令完整参考 |
| | [guide/会话管理.md](guide/会话管理.md) | 自动保存、恢复、Fork 分支 |
| | [guide/工具概览.md](guide/工具概览.md) | 核心工具、插件工具、调用流程 |
| | [guide/记忆系统.md](guide/记忆系统.md) | 跨会话持久化、记忆格式、管理 |
| | [guide/插件系统.md](guide/插件系统.md) | 生命周期钩子、内置插件 |
| | [guide/WebUI手册.md](guide/WebUI手册.md) | 浏览器界面启动、API 参考、WebSocket 协议 |
| | [guide/FAQ.md](guide/FAQ.md) | 10 个常见问题及解答 |
| 🔧 **开发者文档** | [dev/项目概览.md](dev/项目概览.md) | 定位、技术栈、目录结构 |
| | [dev/架构设计.md](dev/架构设计.md) | 分层架构、数据流、生命周期系统 |
| | [dev/引擎.md](dev/引擎.md) | Agent 主干类 API、LifecycleManager |
| | [dev/LLM通信.md](dev/LLM通信.md) | LLM Client、流式、think 标签提取 |
| | [dev/会话模块.md](dev/会话模块.md) | SessionManager API |
| | [dev/配置系统.md](dev/配置系统.md) | 三级优先级、配置项一览 |
| | [dev/显示层.md](dev/显示层.md) | 6 类输出、LLMStreamer、Spinner |
| | [dev/命令系统.md](dev/命令系统.md) | 命令格式、自动发现、开发指南 |
| | [dev/工具系统.md](dev/工具系统.md) | ToolRegistry、核心工具、插件工具开发 |
| | [dev/插件系统.md](dev/插件系统.md) | Plugin 基类、钩子注册、NotificationPlugin |
| | [dev/数据持久化.md](dev/数据持久化.md) | 会话/记忆/任务存储格式 |
| | [dev/中断机制.md](dev/中断机制.md) | Ctrl+C 双重检查、恢复策略 |
| | [dev/自我修改.md](dev/自我修改.md) | 自修改流程、测试、回滚 |
| | [dev/测试.md](dev/测试.md) | 测试体系、会话隔离、防漂移门禁 |
| | [dev/文件命名约定.md](dev/文件命名约定.md) | 「文件即开关」通用规则（Plugin/Skill/Command/Tool 共用） |
| 🔌 **ACP 协议** | [acp/README.md](acp/README.md) | ACP 协议索引 |
| | [acp/使用指南.md](acp/使用指南.md) | IDE 集成指南 |
| | [acp/00-OVERVIEW.md](acp/00-OVERVIEW.md) ~ [07-IMPLEMENTATION.md](acp/07-IMPLEMENTATION.md) | 协议规范全集 |
| 📋 **项目信息** | [CHANGELOG.md](CHANGELOG.md) | 版本历史：0.1.12（迭代记录见 CHANGELOG） |
| | [CONTRIBUTING.md](CONTRIBUTING.md) | PR 流程、代码风格、Commit 规范 |

---

## 🗺️ 文档架构

```
docs/
├── README.md                  ← 本文档 — 文档总索引
│
├── self/                      ← 🤖 自举手册（FP 自己阅读）
│   ├── README.md               分发后环境定位 + 四类扩展总览
│   ├── 扩展命令.md              命令 Command（面向用户，FP 不可主动调用）
│   ├── 扩展工具.md              工具 Tool（FP 主动调用，自举核心）
│   ├── 扩展插件.md              插件 Plugin（后台钩子，慎写）
│   └── 技能与记忆.md            记忆 Memory（FP 主动读写）
│
├── guide/                     ← 📖 使用指南（用户视角）
│   ├── 快速开始.md             从零安装与启动
│   ├── 配置指南.md             config.json + 环境变量
│   ├── CLI入门.md              基本用法、快捷键
│   ├── 命令参考.md              15 个内置命令详解
│   ├── 会话管理.md              自动保存、恢复、Fork
│   ├── 工具概览.md              所有可用工具
│   ├── 记忆系统.md              跨会话记忆
│   ├── 插件系统.md              生命周期钩子、内置插件
│   ├── WebUI手册.md             Web 界面使用
│   └── FAQ.md                  常见问题
│
├── dev/                       ← 🔧 开发者文档（模块视角）
│   ├── 项目概览.md              定位、技术栈、目录结构
│   ├── 架构设计.md              分层架构、数据流
│   ├── 引擎.md                  Agent + LifecycleManager API
│   ├── LLM通信.md               LLM Client 实现
│   ├── 会话模块.md               SessionManager API
│   ├── 配置系统.md               三级优先级
│   ├── 显示层.md                 输出函数、Streamer
│   ├── 命令系统.md               命令格式、开发指南
│   ├── 工具系统.md               ToolRegistry、工具开发
│   ├── 插件系统.md               Plugin 基类、钩子注册
│   ├── 数据持久化.md             存储格式
│   ├── 中断机制.md               Ctrl+C 处理
│   ├── 自我修改.md               自修改流程
│   ├── 测试.md                   测试体系、隔离、防漂移
│   └── 文件命名约定.md            文件即开关（四类扩展共用）
│
├── acp/                       ← 🔌 ACP 协议规范
│   ├── README.md                协议索引
│   ├── 使用指南.md               IDE 集成指南
│   ├── 00-OVERVIEW.md ~ 07-IMPLEMENTATION.md
│
├── CHANGELOG.md                ← 版本历史（符号链接自根目录）
└── CONTRIBUTING.md             ← 贡献指南（符号链接自根目录）
```

> **注**：`prompts/agent.md` 是系统提示词模板，由运行时加载。

---

## 🔗 外部引用

- 根目录 `README.md` 中的文档链接指向本目录下各文件
- 根目录 `CHANGELOG.md` 和 `CONTRIBUTING.md` 已替换为符号链接指向本目录
