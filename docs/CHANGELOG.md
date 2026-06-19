# 更新日志

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/) 和 [Keep a Changelog](https://keepachangelog.com/) 规范。

> **📌 版本号修正说明**
>
> 此前版本号存在虚高（2.0.0/1.1.0/1.0.0 与实际功能成熟度不匹配）。
> 现已统一修正为 `0.1.0`，与 `pyproject.toml` 保持一致。
> 以下版本号保留原始记录以供追溯，实际发布包版本均为 `0.1.0`。

## [0.1.6] — 2026-06-20

### Added

- **PyPI 版本自动检测**: 每次运行 `fp` 时后台线程查询 PyPI JSON API，检测 fp-agent/fp-core/fp-terminal/fp-webui/fp-acp 五个包是否有新版本，发现即提示
- **`/sc` 命令**: 新增短路命令 `/sc`（Short Circuit），一键压缩已完成的对话连通块（连续 closed 消息），提升长会话可读性
- **编号前缀统一**: `/sc` 输出中任务编号使用 `#ID` 前缀，与 display 格式保持一致

### Changed

- **彻底铲除旧技能系统**: 删除 `skills/` 目录全部残留文件及 9 篇文档中对该系统的引用，完成向插件体系的迁移
- **`/sc` 命令主名**: 命令名改为简短 `sc`，移除冗余 aliases
- **`/sc` 范围压缩**: `@M-@N` 格式的连续范围合并为单一压缩符号 `~`

### Fixed

- **`/sc` refiner 数据重复**: 修复 refiner 过程中多次读取导致的数据重复问题
- **`read_file` 截断**: 添加默认截断行为（200 行），防止超大文件一次返回撑爆上下文
- **bash 混合输出策略**: 小输出直接返回，大输出（≥3K）自动保存文件+预览，避免终端缓冲区溢出

## [0.1.5] — 2026-06-17

### Added

- **自举内部文档**: 新增 `fp_core/_self_docs.py` 模块，包含 12 个章节的结构化架构知识（系统概览/生命周期/插件/命令/工具/技能/会话/自修改/配置/LLM抽象/主循环流程图/GitHub资源指引），`pip install fp-core` 后即用，提供 `find_tool()` / `find_command()` / `find_hook()` / `get_summary()` 等辅助函数
- **插件管理技能**: 新增 `plugin_management` 技能，通过重命名文件实现插件的启用/禁用

### Changed

- **docs**: 重命名 `plugins.md` → `文件命名约定.md`，新增四类扩展（插件/命令/工具/技能）对比表
- **docs(plugins)**: 插件输出示例统一使用 `display.info()` 替代 `print()`
- **skills(subagent)**: 精简技能描述，去除冗余内容
- **history**: `print()` 输出改为 `display.info()`/`display.item()`，静默模式下不泄漏输出
- **history**: 命令执行结果返回给 IDE 调用方而非仅打印到终端

### Fixed

- **core**: `rebuild_context()` 改用 `reset()` 替代 `set_system_prompt()`，修复上下文重建时历史残留导致的 prompt 错乱
- **core**: 核弹退出 (`exit!`) 不再残留会话文件
- **subagent**: 真正的静默模式 — 抑制 spinner / LLM 流等 UI 输出，工具结果纯文本化
- **acp**: 改用 `rawInput`/`rawOutput` 符合 ACP v1 规范
- **acp**: 修复并发 prompt 防护、session_id 快照、session-ping 等竞态问题
- **acp**: 修复取消失效、崩溃恢复、毁灭命令过滤等边缘情况

## [0.1.4] — 2026-06-16

### Changed

- **docs**: 快速开始文档初始化方式从"自动生成"改为显式 `fp --init` 命令，CLI 参数列表补全 `--init` 条目

### Fixed

- **config**: `init_config()` 写入 `config.json` 前递归创建父目录，修复配置目录不存在时 `FileNotFoundError` 崩溃（"爷目录不存在"问题）
- **platform_utils**: `find_bash()` 加模块级缓存避免重复扫描 PATH；WSL 空壳 bash 启动器检测（文件大小 + FileDescription）；`BASH_PATH` 配置优先级提升（`config.json` > PATH）；`ansi_supported()` 替换 `colorama` 为 Win32 `GetConsoleMode` API
- **tools/core**: cmd.exe 回退降级时先 `chcp 65001` 切换 UTF-8 代码页；UTF-8 解码出现 `\ufffd`（替换字符）时以 locale 编码重试
- **tools/python_plugin**: 子进程设置 `PYTHONIOENCODING=utf-8` 环境变量，防止 GBK/cp936 locale 下 Unicode 字符解码崩溃

## [0.1.3] — 2026-06-15

### Added

- **跨平台兼容框架**: 新增 `fp_core/platform_utils.py` 模块，统一检测平台类型、定位 Git Bash、适配路径格式
- **Windows 自动路由**: `tools/core.py` 在 Windows 上优先使用 Git Bash 执行命令，无 Git Bash 降级为 `cmd.exe` 并通知 LLM

### Changed

- **路径系统**: 替换全部 6 处硬编码 XDG 路径引用，使用 `platform_utils` 动态适配 Linux/macOS/Windows
- **文档**: 更新 6 份文档，要求 Windows 用户安装 Git for Windows

### Fixed

- **webui**: 修复 `os.chmod` 在 Windows 上不支持的兼容性问题

## [0.1.2] — 2026-06-15

### Fixed

- **版本格式**: `bump_docs.py` 正则改为 `v?[\d.]+`，兼容无 `v` 前缀的版本号
- **文档同步**: `bump_docs.py` 替换后不再残留 `v` 前缀，与 setuptools-scm 保持一致

## [2.0.0] — 2026-06-07

### 🎉 重大重构：生命周期驱动的 Agent 框架

#### 新增

- **生命周期系统**：重构项目为基于 `LifecycleManager` 的插件化架构，23 个生命周期钩子覆盖全流程
  - 初始化阶段：`ON_INIT`, `ON_CONFIG_LOADED`
  - 消息处理：`ON_MESSAGE_RECEIVED`, `ON_MESSAGE_PARSE`, `ON_MESSAGE_FILTER`
  - 执行阶段：`ON_BEFORE_THINK`, `ON_THINK`, `ON_AFTER_THINK`
  - LLM 交互：`ON_BEFORE_LLM_CALL`, `ON_LLM_CALL`, `ON_AFTER_LLM_CALL`
  - 响应阶段：`ON_BEFORE_RESPONSE`, `ON_RESPONSE`, `ON_AFTER_RESPONSE`
  - 工具执行：`ON_TOOL_SELECT`, `ON_TOOL_CALL`, `ON_TOOL_RESULT`, `ON_TOOL_ERROR`
  - 资源管理：`ON_SHUTDOWN`, `ON_CLEANUP`
- **插件自动扫描**："文件即开关"机制，通过文件系统命名约定控制插件启停
  - `name.py` → 启用，`name.py.disabled` → 停用，`_name.py` → 跳过
- **通知插件**：监听 lifecycle 事件，支持桌面通知和声音提醒
- **非流式调用 Spinner 动画**：LLM 响应等待期间显示旋转动画
- **`exit!` 核弹级退出命令**：强制退出而不保存会话

#### 修复

- **Ctrl+C 中断修复**：`signal.signal()` + `_stream_chat` CancelledError 优雅捕获
- **跨平台加固**：signal handler 双保险机制（`all_tasks` + 全局标志）
- **Pylance 类型错误清零**：25 errors → 0，全项目类型安全
- **人称视角混淆**：新增【自我认知规则】，修复第三人称/第二人称混淆
- **`_stream_chat` 非流式化**：修复未绑定变量警告

#### 重构

- **LLM 客户端重构**：外置 `llm/` 模块移至 `fp_core/core/llm_client.py`
- **`_stream_chat` 逻辑重构**：移除流式中间态，统一为整体返回
- **工具系统增强**：改进异常处理和字符串替换验证

### 依赖变更

- `httpx` → 核心依赖，替换 openai SDK
- 新增：`rich`, `wcwidth`（终端显示）
- 新增：`ddgs`（DuckDuckGo 搜索）
- 新增：`prompt_toolkit`（交互式 CLI）
- 新增：`PyYAML`（技能配置解析）

---

## [1.1.0] — 2026-06-01

### 新增

- **Subagent 派遣系统**：支持创建独立子 agent 执行离线任务，含输出契约（静默/调试/格式控制）
- **自我认知规则**：修复 agent 人称视角混淆，所有自言自语句强制使用第一人称
- **工具系统增强**：
  - `web_search` — DuckDuckGo 搜索
  - `web_fetch` — 网页内容抓取
  - 异常处理和字符串替换验证加强

### 修复

- `memory_save_plugin` 参数获取问题
- 自动驱动功能导致的问题（已注释）

### 变更

- 任务系统完全解耦为自包含插件
- 颜色/样式/截断配置移至 `~/.config/fp/config.json`

## [1.0.0] — 2026-05-28

### 重大变更：自实现 LLM 客户端

#### 新增

- **自实现 HTTP 客户端** (`fp_core/core/llm_client.py`)：替换 `openai` SDK，减少外部依赖
- **`display.py` 显示模块**：6 类输出函数 + Spinner + LLMStreamer
- **`<think>` 标签提取**：自动从 LLM 响应中提取 reasoning_content
- **配置管理系统**：三级优先级（JSON > 环境变量 > 默认值）
- **Rich 终端渲染**：替换自定义 Markdown 渲染，支持语法高亮和彩色输出
- **压缩功能**：对话历史智能压缩，控制 token 占用

#### 变更

- 全部 `print()` 替换为分类输出函数
- 配置从硬编码迁移至 `~/.config/fp/config.json` 文件管理

#### 修复

- 25 个 Pylance 类型错误全部修复
- 上下文持久化和工具调用处理重构

## [0.1.0] — 2026-05-27

### ✨ 初次发布

- 五块卵石智能体基础框架
- 基于 openai SDK 的 LLM 客户端
- 基础对话历史管理
- 记忆管理模块 (`memory.py`)
- 技能系统 (`fp_core/skills/`)
- 工具调用系统 (`fp_core/tools/`)
- 命令系统 (`fp_core/commands/`)
- 对话修复与智能压缩

---

## 发布历史

| 0.1.6 | 2026-06-20 | 待补充 |

| 0.1.5 | 2026-06-17 | 待补充 |

| 0.1.4 | 2026-06-16 | 待补充 |

| 0.1.3 | 2026-06-15 | 待补充 |

| 0.1.2 | 2026-06-15 | 待补充 |

| 版本 | 日期 | 摘要 |
|------|------|------|
| 2.0.0 | 2026-06-07 | 生命周期驱动的 Agent 框架重构 |
| 1.1.0 | 2026-06-01 | Subagent 派遣系统 |
| 1.0.0 | 2026-05-28 | 自实现 LLM 客户端 + 显示模块 |
| 0.1.0 | 2026-05-27 | 初始版本 |

> 注：以上版本号为迭代记录标识，实际发布包版本统一为 `0.1.0`（详见顶部说明）。
